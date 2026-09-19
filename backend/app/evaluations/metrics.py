"""Metrics over a corpus run: extraction fields, ranking, and support labels.

Every number here is a comparison between a *generated* prediction and a label
declared independently in ``corpus.py``. None of it recomputes a prediction, and
none of it falls back to a built-in fixture when a prediction is missing — a
missing prediction is a failure sample and a smaller denominator, which is the
only way a rate can stay honest.

Three choices are worth stating because they decide what the numbers mean.

**Extraction is scored on identification, not spelling.** Skill names are compared
case-folded, because normalising ``Python`` to ``python`` is the proofreading
step's job and charging the extractor for it would report a normaliser regression
as an extraction error. What the metric does measure is whether the extractor
found the field at all, and whether it invented one.

**An empty gold field and an empty prediction contribute nothing.** Micro-averaged
counters treat "both absent" as neither a hit nor an error, and the case is still
counted as a sample so a reader can see how much of the corpus had nothing to
find. ``exact_match`` is reported alongside for the scalar fields, where it is the
more legible number.

**Macro-F1 averages over the classes that occur.** A support class that appears
neither in the gold nor in the predictions is excluded rather than scored 0 — the
alternative would let a corpus with no ``PARTIAL`` cases report a lower macro-F1
than one with them, which is a statement about the corpus, not the system.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from backend.app.candidates.schemas import CandidateProfileDraft
from backend.app.evaluations.corpus import CorpusCase, EvaluationCorpus, Split
from backend.app.evaluations.runner import CasePrediction, CorpusRun, PredictionSource
from backend.app.infrastructure.chat_completion import UsageRecord
from backend.app.retrieval.models import HardRuleId

#: Bound on the failure list. A run with hundreds of failures reports the count
#: and the first few; the full set is a query away, not a paragraph to scroll.
MAX_FAILURE_SAMPLES = 40


class Stage(StrEnum):
    """Which part of the pipeline produced a failure sample."""

    RUN = "RUN"
    EXTRACTION = "EXTRACTION"
    RETRIEVAL = "RETRIEVAL"
    OUTCOME = "OUTCOME"
    SUPPORT = "SUPPORT"


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

#: The fields the extraction metric scores. ``skills`` is a set; the other two are
#: scalars, so their precision/recall degenerate to exact-match accuracy and are
#: reported with ``exact_match`` alongside.
EXTRACTION_FIELDS: tuple[str, ...] = ("full_name", "education_level", "skills")


def _extraction_sets(
    draft: CandidateProfileDraft, gold: CorpusCase
) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """``{field: (gold_set, predicted_set)}`` for one case."""
    predicted_name = draft.full_name.strip() if draft.full_name else ""
    predicted_education = draft.education_level or ""
    predicted_skills = frozenset(
        claim.name.strip().casefold() for claim in draft.skills if claim.name
    )
    return {
        "full_name": (
            _singleton(gold.extraction.full_name),
            _singleton(predicted_name),
        ),
        "education_level": (
            _singleton(gold.extraction.education_level),
            _singleton(predicted_education),
        ),
        "skills": (
            frozenset(skill.casefold() for skill in gold.extraction.skills),
            predicted_skills,
        ),
    }


def _singleton(value: str | None) -> frozenset[str]:
    text = (value or "").strip().casefold()
    return frozenset({text}) if text else frozenset()


@dataclass(frozen=True, slots=True)
class FieldScore:
    """Micro-averaged precision/recall/F1 for one extracted field."""

    field: str
    samples: int
    exact_matches: int
    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def precision(self) -> float:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        precision, recall = self.precision, self.recall
        return 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    @property
    def exact_match(self) -> float:
        return self.exact_matches / self.samples if self.samples else 0.0


@dataclass(frozen=True, slots=True)
class ExtractionReport:
    """Field-level extraction quality plus how much of the corpus was measurable."""

    fields: tuple[FieldScore, ...]
    failed_cases: int
    measured_cases: int

    @property
    def overall_f1(self) -> float:
        """Mean field F1. A single headline number, never a substitute for the table."""
        return sum(item.f1 for item in self.fields) / len(self.fields) if self.fields else 0.0


def _score_field(field: str, samples: list[tuple[frozenset[str], frozenset[str]]]) -> FieldScore:
    true_positives = false_positives = false_negatives = exact = 0
    for gold_set, predicted_set in samples:
        true_positives += len(gold_set & predicted_set)
        false_positives += len(predicted_set - gold_set)
        false_negatives += len(gold_set - predicted_set)
        exact += 1 if gold_set == predicted_set else 0
    return FieldScore(
        field=field,
        samples=len(samples),
        exact_matches=exact,
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
    )


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FamilyScore:
    """Ranking quality for one posting."""

    job_family: str
    k: int
    relevant: int
    pool_size: int
    retrieved_at_k: int
    first_relevant_rank: int | None
    ndcg_at_k: float

    @property
    def recall_at_k(self) -> float:
        return self.retrieved_at_k / self.relevant if self.relevant else 0.0

    @property
    def max_recall_at_k(self) -> float:
        """The highest recall this posting's relevance set allows at ``k``.

        With ten relevant candidates and ``k = 5``, recall cannot exceed 0.5 no
        matter how good the ranking is. Reporting recall without this ceiling
        invites a reader to treat 0.45 as a near-failure when it is 90% of what is
        reachable — the number is not wrong, it is uninterpretable alone.
        """
        return min(self.relevant, self.k) / self.relevant if self.relevant else 0.0

    @property
    def reciprocal_rank(self) -> float:
        return 1.0 / self.first_relevant_rank if self.first_relevant_rank else 0.0


@dataclass(frozen=True, slots=True)
class RetrievalReport:
    """Recall@K, MRR and nDCG averaged over postings, with the per-posting rows.

    The mean is the headline; the rows are what makes it checkable, because a mean
    over four postings hides a posting where nothing relevant was retrieved.
    """

    k: int
    families: tuple[FamilyScore, ...]

    @property
    def recall_at_k(self) -> float:
        return _mean([item.recall_at_k for item in self.families])

    @property
    def recall_ceiling(self) -> float:
        """The mean ceiling recall is measured against. Reported next to it."""
        return _mean([item.max_recall_at_k for item in self.families])

    @property
    def mrr(self) -> float:
        return _mean([item.reciprocal_rank for item in self.families])

    @property
    def ndcg_at_k(self) -> float:
        return _mean([item.ndcg_at_k for item in self.families])


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _ndcg(ranked: Sequence[UUID], relevant: set[UUID], k: int) -> float:
    """Binary-gain nDCG@K with the standard ``log2(rank + 1)`` discount."""
    gains = [1.0 if profile_id in relevant else 0.0 for profile_id in ranked[:k]]
    dcg = sum(gain / math.log2(index + 1) for index, gain in enumerate(gains, start=1))
    ideal = [1.0] * min(len(relevant), k)
    idcg = sum(gain / math.log2(index + 1) for index, gain in enumerate(ideal, start=1))
    return dcg / idcg if idcg else 0.0


# --------------------------------------------------------------------------- #
# Support labels and hard-rule outcomes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ClassScore:
    """One-vs-rest precision/recall/F1 for a single support label."""

    label: str
    gold: int
    predicted: int
    true_positives: int

    @property
    def precision(self) -> float:
        return self.true_positives / self.predicted if self.predicted else 0.0

    @property
    def recall(self) -> float:
        return self.true_positives / self.gold if self.gold else 0.0

    @property
    def f1(self) -> float:
        precision, recall = self.precision, self.recall
        return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


@dataclass(frozen=True, slots=True)
class SupportReport:
    """Support-label agreement, the metric the old semantic suite faked."""

    classes: tuple[ClassScore, ...]
    samples: int

    @property
    def macro_f1(self) -> float:
        return _mean([item.f1 for item in self.classes])

    @property
    def macro_precision(self) -> float:
        return _mean([item.precision for item in self.classes])

    @property
    def accuracy(self) -> float:
        return (
            sum(item.true_positives for item in self.classes) / self.samples
            if self.samples
            else 0.0
        )


@dataclass(frozen=True, slots=True)
class RuleScore:
    """Hard-rule verdict agreement for one rule."""

    rule_id: str
    samples: int
    agreements: int

    @property
    def accuracy(self) -> float:
        return self.agreements / self.samples if self.samples else 0.0


@dataclass(frozen=True, slots=True)
class OutcomeReport:
    """Whether the deterministic verdicts match the declared ones.

    Not a gate: the hard rules are the part of the system that is *supposed* to be
    deterministic, so a mismatch here means the corpus or the spec moved, not that
    the model was unlucky.
    """

    rules: tuple[RuleScore, ...]

    @property
    def accuracy(self) -> float:
        return _mean([item.accuracy for item in self.rules])


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FailureSample:
    """One concrete disagreement, named so it can be looked up."""

    case_id: str
    stage: Stage
    subject: str
    expected: str
    observed: str


# --------------------------------------------------------------------------- #
# The aggregate
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CorpusMetrics:
    """One run's metrics, tagged with where its predictions came from.

    The tag is not metadata: two ``CorpusMetrics`` with different ``source`` values
    describe different things and must never be averaged, so
    :func:`combine` refuses to.
    """

    source: PredictionSource
    corpus_name: str
    corpus_version: str
    content_hash: str
    split: Split | None
    model_name: str
    gateway_version: str
    embedding_version: str
    prompt_version: str
    rule_version: str
    top_k: int
    pool_size: int
    total_cases: int
    measured_cases: int
    failed_cases: int
    extraction: ExtractionReport
    retrieval: RetrievalReport
    support: SupportReport
    outcome: OutcomeReport
    failures: tuple[FailureSample, ...]
    failure_count: int
    elapsed_seconds: float
    usage: UsageRecord | None

    @property
    def concluded(self) -> bool:
        """False when the run measured nothing, so it cannot report a pass.

        A failed case is a smaller denominator rather than a wrong number, so a
        partial run still concludes. A run with *no* measured case does not: there
        is no number to be right or wrong about.
        """
        return self.measured_cases > 0


def combine(left: CorpusMetrics, right: CorpusMetrics) -> CorpusMetrics:
    """Refuse to merge metrics from different prediction sources.

    Averaging a replayed recording with a live call produces a figure that
    describes no run that ever happened. Raising is the only honest behaviour: the
    caller that wanted a combined number wants something that cannot exist.
    """
    raise ValueError(
        "refusing to combine metrics from different prediction sources "
        f"({left.source.value} and {right.source.value}); report them separately"
    )


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def evaluate(
    run: CorpusRun,
    corpus: EvaluationCorpus,
    *,
    k: int,
    split: Split | None = None,
) -> CorpusMetrics:
    """Compare a run's predictions against the corpus's declared labels.

    ``split`` restricts the *per-case* metrics — extraction, support labels and
    hard-rule outcomes — to one half of the corpus. The holdout half is what a
    threshold may be reported against; the dev half is what it may be tuned on.
    Passing ``None`` scores every case, which is useful while iterating and must
    not be what a published number uses.

    Retrieval deliberately ignores ``split``. A ranking is a property of the whole
    candidate pool, so ranking half of it produces a ranking production never
    makes — and cutting the pool to five candidates per posting makes Recall@K
    trivially 1.0, which measures the cut rather than the retriever. Relevance is
    also hand-declared rather than tuned, so there is nothing for the split to
    protect here.
    """
    cases = {case.case_id: case for case in corpus.cases}
    predictions = [item for item in run.cases if split is None or item.split is split]
    measured = [item for item in predictions if item.measured]
    failures: list[FailureSample] = []
    failure_count = 0

    def record(sample: FailureSample) -> None:
        nonlocal failure_count
        failure_count += 1
        if len(failures) < MAX_FAILURE_SAMPLES:
            failures.append(sample)

    extraction = _extraction_report(measured, cases, record)
    outcome = _outcome_report(measured, cases, record)
    support = _support_report(measured, cases, record)
    # Every measured case, not just the split: see the docstring.
    retrieval = _retrieval_report([item for item in run.cases if item.measured], cases, k, record)

    for item in predictions:
        if not item.measured:
            record(
                FailureSample(
                    case_id=item.case_id,
                    stage=Stage.RUN,
                    subject="extraction",
                    expected="a prediction",
                    observed=item.error_code or "unknown",
                )
            )

    return CorpusMetrics(
        source=run.source,
        corpus_name=run.corpus_name,
        corpus_version=run.corpus_version,
        content_hash=run.content_hash,
        split=split,
        model_name=run.model_name,
        gateway_version=run.gateway_version,
        embedding_version=run.embedding_version,
        prompt_version=run.prompt_version,
        rule_version=run.rule_version,
        top_k=k,
        pool_size=run.pool_size,
        total_cases=len(predictions),
        measured_cases=len(measured),
        failed_cases=len(predictions) - len(measured),
        extraction=extraction,
        retrieval=retrieval,
        support=support,
        outcome=outcome,
        failures=tuple(failures),
        failure_count=failure_count,
        elapsed_seconds=run.elapsed_seconds,
        usage=run.usage,
    )


def _extraction_report(
    measured: list[CasePrediction],
    cases: dict[str, CorpusCase],
    record: Callable[[FailureSample], None],
) -> ExtractionReport:
    per_field: dict[str, list[tuple[frozenset[str], frozenset[str]]]] = {
        field: [] for field in EXTRACTION_FIELDS
    }
    for item in measured:
        if item.draft is None:
            continue
        gold = cases[item.case_id]
        for field, pair in _extraction_sets(item.draft, gold).items():
            per_field[field].append(pair)
            gold_set, predicted_set = pair
            if gold_set != predicted_set:
                record(
                    FailureSample(
                        case_id=item.case_id,
                        stage=Stage.EXTRACTION,
                        subject=field,
                        expected=",".join(sorted(gold_set)) or "(empty)",
                        observed=",".join(sorted(predicted_set)) or "(empty)",
                    )
                )
    return ExtractionReport(
        fields=tuple(_score_field(field, per_field[field]) for field in EXTRACTION_FIELDS),
        failed_cases=sum(1 for item in measured if item.draft is None),
        measured_cases=len(measured),
    )


def _outcome_report(
    measured: list[CasePrediction],
    cases: dict[str, CorpusCase],
    record: Callable[[FailureSample], None],
) -> OutcomeReport:
    tally: dict[str, list[int]] = {rule.value: [0, 0] for rule in HardRuleId}
    for item in measured:
        gold = cases[item.case_id].gold_by_claim_type
        for rule in item.bundle.rules if item.bundle else []:
            expected = gold[f"hard_rule:{rule.rule_id.value}"].expected_outcome
            counters = tally[rule.rule_id.value]
            counters[0] += 1
            if rule.result == expected:
                counters[1] += 1
            else:
                record(
                    FailureSample(
                        case_id=item.case_id,
                        stage=Stage.OUTCOME,
                        subject=rule.rule_id.value,
                        expected=expected.value,
                        observed=rule.result.value,
                    )
                )
    return OutcomeReport(
        rules=tuple(
            RuleScore(rule_id=rule_id, samples=counters[0], agreements=counters[1])
            for rule_id, counters in tally.items()
        )
    )


def _support_report(
    measured: list[CasePrediction],
    cases: dict[str, CorpusCase],
    record: Callable[[FailureSample], None],
) -> SupportReport:
    gold_counts: dict[str, int] = {}
    predicted_counts: dict[str, int] = {}
    true_positives: dict[str, int] = {}
    samples = 0
    for item in measured:
        gold = cases[item.case_id].gold_by_claim_type
        observed = {view.claim.claim_type: view.claim.support_level for view in item.claims}
        for claim_type, expectation in gold.items():
            samples += 1
            expected = expectation.expected_support.value
            actual = observed.get(claim_type)
            gold_counts[expected] = gold_counts.get(expected, 0) + 1
            if actual is None:
                record(
                    FailureSample(
                        case_id=item.case_id,
                        stage=Stage.SUPPORT,
                        subject=claim_type,
                        expected=expected,
                        observed="(no claim)",
                    )
                )
                continue
            predicted_counts[actual.value] = predicted_counts.get(actual.value, 0) + 1
            if actual.value == expected:
                true_positives[expected] = true_positives.get(expected, 0) + 1
            else:
                record(
                    FailureSample(
                        case_id=item.case_id,
                        stage=Stage.SUPPORT,
                        subject=claim_type,
                        expected=expected,
                        observed=actual.value,
                    )
                )
    labels = sorted(set(gold_counts) | set(predicted_counts))
    return SupportReport(
        classes=tuple(
            ClassScore(
                label=label,
                gold=gold_counts.get(label, 0),
                predicted=predicted_counts.get(label, 0),
                true_positives=true_positives.get(label, 0),
            )
            for label in labels
        ),
        samples=samples,
    )


def _retrieval_report(
    measured: list[CasePrediction],
    cases: dict[str, CorpusCase],
    k: int,
    record: Callable[[FailureSample], None],
) -> RetrievalReport:
    """One row per posting, using that posting's own fused ranking.

    Every case in a family is ranked by the same query over the same pool, so the
    first measured case of a family supplies the ranking for all of them. The
    relevance set is the family's own cases flagged ``relevant``.
    """
    by_family: dict[str, list[CasePrediction]] = {}
    for item in measured:
        by_family.setdefault(item.job_family, []).append(item)

    rows: list[FamilyScore] = []
    for family, items in sorted(by_family.items()):
        ranking = next((item.ranked_profile_ids for item in items if item.ranked_profile_ids), ())
        relevant = {
            cases[item.case_id].profile_id for item in items if cases[item.case_id].relevant
        }
        if not relevant:
            # No relevant case means recall has no denominator; reporting 0.0 would
            # read as "retrieved nothing" rather than "nothing to retrieve".
            continue
        first_rank = next(
            (index for index, profile_id in enumerate(ranking, start=1) if profile_id in relevant),
            None,
        )
        retrieved = len(set(ranking[:k]) & relevant)
        if first_rank is None or first_rank > k:
            record(
                FailureSample(
                    case_id=f"{family}@ranking",
                    stage=Stage.RETRIEVAL,
                    subject=f"top-{k}",
                    expected=f"{len(relevant)} relevant",
                    observed=f"{retrieved} retrieved",
                )
            )
        rows.append(
            FamilyScore(
                job_family=family,
                k=k,
                relevant=len(relevant),
                pool_size=len(items),
                retrieved_at_k=retrieved,
                first_relevant_rank=first_rank,
                ndcg_at_k=_ndcg(ranking, relevant, k),
            )
        )
    return RetrievalReport(k=k, families=tuple(rows))


__all__ = [
    "EXTRACTION_FIELDS",
    "MAX_FAILURE_SAMPLES",
    "ClassScore",
    "CorpusMetrics",
    "ExtractionReport",
    "FailureSample",
    "FamilyScore",
    "FieldScore",
    "OutcomeReport",
    "RetrievalReport",
    "RuleScore",
    "Stage",
    "SupportReport",
    "combine",
    "evaluate",
]
