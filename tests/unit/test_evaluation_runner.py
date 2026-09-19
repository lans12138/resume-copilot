"""The evaluation runner, its metrics, and the report that keeps sources apart.

Three things are being pinned here, and only the first is ordinary.

**The pipeline is really run.** Extraction goes through the configured gateway on
the case's raw text, recall through the real service, hard rules through the real
evaluator, and the report through the real builder. A test that stubbed any of
those would be testing the stub.

**A run that cannot measure cannot conclude.** An empty corpus, a recording that
misses an input, or a run where every case failed must fail loudly. The failure
mode being guarded against is a gate that reports "passed" because it measured
nothing.

**The declared labels agree with the implementation.** The corpus's support labels
were derived by hand from §9.4 and §4.5; this file is where that derivation is
checked against what the pipeline actually produces. It is the assertion the whole
work package exists to make possible.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from backend.app.candidates.schemas import CandidateProfileDraft, SkillClaim
from backend.app.documents.parsers import ParsedBlock
from backend.app.evaluations.corpus import BUILTIN_CORPUS, CaseTag, Split
from backend.app.evaluations.metrics import (
    ClassScore,
    _ndcg,
    _score_field,
    combine,
    evaluate,
)
from backend.app.evaluations.report import (
    SOURCE_CAVEATS,
    SOURCE_LABELS,
    FixtureMetric,
    Pricing,
    Section,
    build_report,
    render,
)
from backend.app.evaluations.runner import (
    EMPTY_CORPUS,
    INCOMPLETE_RECORDING,
    CorpusRepository,
    CorpusRun,
    EvaluationSetupError,
    PredictionSource,
    blocks_for,
    chunks_for,
    classify_gateway,
    content_hash,
    run_corpus,
)
from backend.app.infrastructure.embedding import FakeEmbeddingGateway
from backend.app.infrastructure.model_gateway import FakeModelGateway
from backend.app.infrastructure.recording import RecordedModelGateway, Recording
from backend.app.reports.models import SupportLevel
from backend.app.retrieval.models import RetrievalConfig

_CONFIG = RetrievalConfig(
    structured_weight=1.0,
    keyword_weight=1.0,
    vector_weight=1.0,
    rrf_k=60,
    top_k=5,
    rule_version="eval-r1",
)

#: Running the whole corpus takes a fraction of a second, but four tests need it,
#: so it is built once. Keyed by nothing: the inputs are constant by construction.
_RUN_CACHE: dict[str, CorpusRun] = {}


class _SpyGateway:
    """A gateway that records what it was asked, and extracts nothing."""

    version = "spy-v1"

    def __init__(self) -> None:
        self.seen_texts: list[str] = []
        self.seen_block_counts: list[int] = []

    async def extract_profile(
        self, *, full_text: str, blocks: Sequence[ParsedBlock]
    ) -> CandidateProfileDraft:
        self.seen_texts.append(full_text)
        self.seen_block_counts.append(len(blocks))
        return CandidateProfileDraft()


class _FailingGateway:
    """A gateway whose every call fails, standing in for a provider outage."""

    version = "failing-v1"

    async def extract_profile(
        self, *, full_text: str, blocks: Sequence[ParsedBlock]
    ) -> CandidateProfileDraft:
        raise RuntimeError("provider unavailable")


def _run_builtin() -> CorpusRun:
    cached = _RUN_CACHE.get("builtin")
    if cached is None:
        cached = asyncio.run(
            run_corpus(
                corpus=BUILTIN_CORPUS,
                cases=BUILTIN_CORPUS.cases,
                gateway=FakeModelGateway(),
                embedding_gateway=FakeEmbeddingGateway(dimension=64),
                config=_CONFIG,
            )
        )
        _RUN_CACHE["builtin"] = cached
    return cached


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


def test_classify_gateway_names_the_source_from_the_gateway() -> None:
    assert classify_gateway(FakeModelGateway()) is PredictionSource.FAKE_MODEL
    recording = Recording(dataset_name="d", dataset_version="v1")
    assert classify_gateway(RecordedModelGateway(recording)) is PredictionSource.RECORDED_REPLAY
    # Anything that is not one of the two known adapters is a live call: there is
    # no third adapter, so guessing "fake" would understate what a run measured.
    assert classify_gateway(_SpyGateway()) is PredictionSource.LIVE_MODEL


def test_every_source_has_a_label_and_a_caveat() -> None:
    """A number without its scope is the failure this package exists to correct."""
    assert set(SOURCE_LABELS) == set(PredictionSource)
    assert set(SOURCE_CAVEATS) == set(PredictionSource)
    assert all(text.strip() for text in SOURCE_CAVEATS.values())


# --------------------------------------------------------------------------- #
# Inputs handed to the pipeline
# --------------------------------------------------------------------------- #


def test_chunks_cover_every_section_in_order() -> None:
    case = BUILTIN_CORPUS.cases[0]
    chunks = chunks_for(case)
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(case.sections)))
    assert [chunk.text for chunk in chunks] == [text for _section, text in case.sections]
    assert [chunk.section_type for chunk in chunks] == [section for section, _text in case.sections]
    assert {chunk.candidate_profile_id for chunk in chunks} == {case.profile_id}
    assert len({chunk.document_id for chunk in chunks}) == 1


def test_blocks_reconstruct_the_resume_text() -> None:
    """The blocks a real extraction call receives must describe the same document."""
    for case in BUILTIN_CORPUS.cases[:5]:
        blocks = blocks_for(case)
        assert [block.text for block in blocks] == [text for _section, text in case.sections]
        assert all(block.text in case.resume_text for block in blocks)


def test_content_hash_covers_the_inputs_and_the_gold() -> None:
    """A hash over a manifest would say "same description", not "same evaluation"."""
    baseline = content_hash(BUILTIN_CORPUS)
    assert baseline == content_hash(BUILTIN_CORPUS)

    first = BUILTIN_CORPUS.cases[0]
    flipped = replace(
        first,
        conclusions=tuple(
            replace(item, expected_support=SupportLevel.INSUFFICIENT)
            if item.expected_support is SupportLevel.SUPPORTED
            else item
            for item in first.conclusions
        ),
    )
    gold_changed = replace(BUILTIN_CORPUS, cases=(flipped, *BUILTIN_CORPUS.cases[1:]))

    reworded = replace(first, sections=(*first.sections, ("extra", "补充一行")))
    input_changed = replace(BUILTIN_CORPUS, cases=(reworded, *BUILTIN_CORPUS.cases[1:]))

    assert content_hash(gold_changed) != baseline
    assert content_hash(input_changed) != baseline
    assert content_hash(input_changed) != content_hash(gold_changed)


def test_a_case_note_is_not_part_of_the_dataset_identity() -> None:
    """A clarified comment must not look like a new dataset version."""
    first = BUILTIN_CORPUS.cases[0]
    renoted = replace(first, note=first.note + "（补充说明）")
    altered = replace(BUILTIN_CORPUS, cases=(renoted, *BUILTIN_CORPUS.cases[1:]))
    assert content_hash(altered) == content_hash(BUILTIN_CORPUS)


def test_corpus_repository_serves_only_the_profiles_it_was_built_with() -> None:
    case = BUILTIN_CORPUS.cases[0]
    repository = CorpusRepository(
        profiles=(case.ready_profile(),),
        jobs={case.job.job_version_id: case.job},
        vectors=(),
    )
    assert asyncio.run(repository.list_ready_profiles()) == [case.ready_profile()]
    assert asyncio.run(repository.list_chunk_vectors([case.profile_id])) == []
    assert asyncio.run(repository.get_job_query(case.job.job_version_id)) == case.job
    with pytest.raises(ValueError):
        asyncio.run(repository.get_job_query(uuid4()))


# --------------------------------------------------------------------------- #
# Setup failures
# --------------------------------------------------------------------------- #


def test_an_empty_corpus_is_refused() -> None:
    with pytest.raises(EvaluationSetupError) as error:
        asyncio.run(
            run_corpus(
                corpus=BUILTIN_CORPUS,
                cases=[],
                gateway=FakeModelGateway(),
                embedding_gateway=FakeEmbeddingGateway(dimension=64),
                config=_CONFIG,
            )
        )
    assert error.value.code == EMPTY_CORPUS


def test_a_recording_that_misses_an_input_is_refused_before_any_metric() -> None:
    """A missing fixture is an incomplete evaluation, not a low score."""
    gateway = RecordedModelGateway(Recording(dataset_name="d", dataset_version="v1"))
    with pytest.raises(EvaluationSetupError) as error:
        asyncio.run(
            run_corpus(
                corpus=BUILTIN_CORPUS,
                cases=BUILTIN_CORPUS.cases,
                gateway=gateway,
                embedding_gateway=FakeEmbeddingGateway(dimension=64),
                config=_CONFIG,
            )
        )
    assert error.value.code == INCOMPLETE_RECORDING


def test_a_run_where_every_case_failed_does_not_conclude() -> None:
    run = asyncio.run(
        run_corpus(
            corpus=BUILTIN_CORPUS,
            cases=BUILTIN_CORPUS.cases,
            gateway=_FailingGateway(),
            embedding_gateway=FakeEmbeddingGateway(dimension=64),
            config=_CONFIG,
        )
    )
    metrics = evaluate(run, BUILTIN_CORPUS, k=5)
    assert metrics.measured_cases == 0
    assert not metrics.concluded
    assert metrics.failed_cases == len(BUILTIN_CORPUS.cases)


# --------------------------------------------------------------------------- #
# What the pipeline is actually asked
# --------------------------------------------------------------------------- #


def test_extraction_receives_the_raw_resume_text_and_its_blocks() -> None:
    spy = _SpyGateway()
    cases = BUILTIN_CORPUS.cases[:3]
    asyncio.run(
        run_corpus(
            corpus=BUILTIN_CORPUS,
            cases=cases,
            gateway=spy,
            embedding_gateway=FakeEmbeddingGateway(dimension=64),
            config=_CONFIG,
        )
    )
    assert spy.seen_texts == [case.resume_text for case in cases]
    assert spy.seen_block_counts == [len(case.sections) for case in cases]


def test_a_failing_case_is_recorded_rather_than_aborting_the_run() -> None:
    """Dropping the case silently would inflate every rate."""
    cases = BUILTIN_CORPUS.cases[:2]
    run = asyncio.run(
        run_corpus(
            corpus=BUILTIN_CORPUS,
            cases=cases,
            gateway=_FailingGateway(),
            embedding_gateway=FakeEmbeddingGateway(dimension=64),
            config=_CONFIG,
        )
    )
    assert len(run.cases) == len(cases)
    assert all(not case.measured for case in run.cases)
    assert all(
        case.error_code and case.error_code.startswith("EVALUATION_EXTRACTION_FAILED")
        for case in run.cases
    )
    metrics = evaluate(run, BUILTIN_CORPUS, k=5)
    assert metrics.failure_count == len(cases)


def test_a_gateway_without_a_usage_counter_reports_usage_as_unknown() -> None:
    """Zero would read as "this run was free"; None reads as "cannot be priced"."""
    run = _run_builtin()
    assert run.usage is None
    assert run.source is PredictionSource.FAKE_MODEL


# --------------------------------------------------------------------------- #
# Metric arithmetic
# --------------------------------------------------------------------------- #


def test_ndcg_is_one_for_a_perfect_ranking() -> None:
    relevant = {uuid4(), uuid4()}
    assert _ndcg(tuple(relevant), relevant, 2) == pytest.approx(1.0)


def test_ndcg_discounts_a_relevant_hit_that_arrives_late() -> None:
    second, filler = uuid4(), uuid4()
    relevant = {second}
    perfect = _ndcg((second, filler), relevant, 2)
    delayed = _ndcg((filler, second), relevant, 2)
    assert perfect == pytest.approx(1.0)
    assert delayed < perfect


def test_ndcg_ignores_hits_beyond_the_cut() -> None:
    relevant = {uuid4()}
    filler = uuid4()
    assert _ndcg((filler, *relevant), relevant, 1) == 0.0


def test_field_score_reports_precision_recall_and_exact_match() -> None:
    score = _score_field(
        "skills",
        [
            (frozenset({"python", "sql"}), frozenset({"python", "sql"})),
            (frozenset({"java"}), frozenset({"java", "spring"})),
            (frozenset({"go"}), frozenset()),
        ],
    )
    assert score.samples == 3
    assert score.exact_matches == 1
    assert score.true_positives == 3
    assert score.false_positives == 1
    assert score.false_negatives == 1
    assert score.precision == pytest.approx(0.75)
    assert score.recall == pytest.approx(0.75)
    assert score.f1 == pytest.approx(0.75)
    assert score.exact_match == pytest.approx(1 / 3)


def test_field_score_is_zero_rather_than_undefined_with_nothing_to_score() -> None:
    score = _score_field("full_name", [(frozenset(), frozenset())])
    assert (score.precision, score.recall, score.f1) == (0.0, 0.0, 0.0)
    # Both absent is neither a hit nor an error, but it is still a sample: a reader
    # has to be able to see how much of the corpus had nothing to find.
    assert score.samples == 1
    assert score.exact_matches == 1
    assert score.exact_match == pytest.approx(1.0)


def test_class_score_arithmetic() -> None:
    score = ClassScore(label="PARTIAL", gold=4, predicted=5, true_positives=3)
    assert score.precision == pytest.approx(0.6)
    assert score.recall == pytest.approx(0.75)
    assert score.f1 == pytest.approx(2 * 0.6 * 0.75 / 1.35)


def test_combining_different_sources_is_refused() -> None:
    metrics = evaluate(_run_builtin(), BUILTIN_CORPUS, k=5)
    with pytest.raises(ValueError, match="refusing to combine"):
        combine(metrics, metrics)


# --------------------------------------------------------------------------- #
# The declared labels against the real pipeline
# --------------------------------------------------------------------------- #


def test_the_corpus_support_labels_match_what_the_pipeline_produces() -> None:
    """The corpus's labels were derived by hand from §9.4 and §4.5.

    This is where that derivation meets the implementation. A failure here means
    one of the two moved, which is exactly the signal a hand-declared label set is
    for — and the opposite of the old suite, whose gold was a copy of its
    predictions and therefore could not fail at all.
    """
    metrics = evaluate(_run_builtin(), BUILTIN_CORPUS, k=5)
    assert metrics.support.samples == len(BUILTIN_CORPUS.cases) * 4
    assert metrics.support.macro_f1 == pytest.approx(1.0)
    assert metrics.support.accuracy == pytest.approx(1.0)


def test_the_hard_rule_verdicts_match_the_declared_ones() -> None:
    metrics = evaluate(_run_builtin(), BUILTIN_CORPUS, k=5)
    assert metrics.outcome.accuracy == pytest.approx(1.0)
    assert {rule.samples for rule in metrics.outcome.rules} == {len(BUILTIN_CORPUS.cases)}


def test_the_only_extraction_gap_is_the_one_the_corpus_declares() -> None:
    """Every failure must be attributable, or the metric is not measuring anything."""
    run = _run_builtin()
    metrics = evaluate(run, BUILTIN_CORPUS, k=5)
    skills = next(item for item in metrics.extraction.fields if item.field == "skills")
    assert skills.precision == pytest.approx(1.0)
    assert 0.0 < skills.recall < 1.0

    failing = {sample.case_id for sample in metrics.failures if sample.subject == "skills"}
    expected = {
        case.case_id
        for case in BUILTIN_CORPUS.cases
        if "out-of-vocabulary-skills" in case.archetype
    }
    assert failing == expected


def test_the_split_narrows_every_metric_including_the_ranking() -> None:
    """The reported half is scored end to end; a metric that ignored the split would
    describe a set the report does not name."""
    run = _run_builtin()
    whole = evaluate(run, BUILTIN_CORPUS, k=5)
    holdout = evaluate(run, BUILTIN_CORPUS, k=5, split=Split.HOLDOUT)

    assert holdout.split is Split.HOLDOUT
    assert holdout.measured_cases < whole.measured_cases
    assert holdout.support.samples < whole.support.samples
    assert holdout.retrieval.families and whole.retrieval.families
    assert {row.pool_size for row in holdout.retrieval.families} != {
        row.pool_size for row in whole.retrieval.families
    }
    assert holdout.retrieval.families[0].relevant < whole.retrieval.families[0].relevant


def test_recall_is_reported_with_the_ceiling_it_is_measured_against() -> None:
    """Ten relevant candidates and K=5 cap recall at 0.5; unlabelled it reads as failure."""
    metrics = evaluate(_run_builtin(), BUILTIN_CORPUS, k=5)
    assert metrics.retrieval.recall_ceiling == pytest.approx(0.5)
    assert metrics.retrieval.recall_at_k <= metrics.retrieval.recall_ceiling
    for family in metrics.retrieval.families:
        assert family.max_recall_at_k == pytest.approx(min(family.relevant, 5) / family.relevant)


def test_the_holdout_half_still_reaches_every_support_class_it_declares() -> None:
    """The reported half must exercise all three levels, or two go unmeasured."""
    holdout = evaluate(_run_builtin(), BUILTIN_CORPUS, k=5, split=Split.HOLDOUT)
    assert {item.label for item in holdout.support.classes} == {
        "SUPPORTED",
        "PARTIAL",
        "INSUFFICIENT",
    }


def test_every_declared_case_is_scored_by_the_run() -> None:
    run = _run_builtin()
    assert {case.case_id for case in run.cases} == {case.case_id for case in BUILTIN_CORPUS.cases}
    assert {tag for case in BUILTIN_CORPUS.cases for tag in case.tags} == set(CaseTag)


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def _section(source: PredictionSource, heading: str = "h") -> Section:
    return Section(source=source, heading=heading, caveat=SOURCE_CAVEATS[source])


def test_a_report_refuses_two_sections_for_one_source() -> None:
    with pytest.raises(ValueError, match="more than one section"):
        build_report(
            "t",
            [
                _section(PredictionSource.LIVE_MODEL),
                _section(PredictionSource.LIVE_MODEL),
            ],
        )


def test_each_section_is_printed_under_its_own_label_and_caveat() -> None:
    metrics = evaluate(_run_builtin(), BUILTIN_CORPUS, k=5)
    report = build_report(
        "报告",
        [
            Section(
                source=metrics.source,
                heading="corpus@v1｜HOLDOUT",
                caveat=SOURCE_CAVEATS[metrics.source],
                metrics=metrics,
            ),
            Section(
                source=PredictionSource.SCORER_FIXTURE,
                heading="内置套件",
                caveat=SOURCE_CAVEATS[PredictionSource.SCORER_FIXTURE],
                fixtures=(
                    FixtureMetric(name="recall_at_k", value=0.9, threshold=0.85, passed=True),
                ),
            ),
        ],
    )
    text = render(report)
    for source in (metrics.source, PredictionSource.SCORER_FIXTURE):
        assert SOURCE_LABELS[source] in text
        assert SOURCE_CAVEATS[source] in text
    assert "召回率" in text and "宏平均 F1" in text and "上限" in text
    assert "0.9000" in text and "0.8500" in text


def test_cost_is_not_estimated_without_a_recorded_price_version() -> None:
    metrics = evaluate(_run_builtin(), BUILTIN_CORPUS, k=5)
    text = render(
        build_report(
            "t",
            [Section(source=metrics.source, heading="h", caveat="c", metrics=metrics)],
        )
    )
    assert "费用：**未估算**" in text
    assert "Token 用量：本网关不上报用量" in text


def test_cost_is_estimated_only_from_an_explicit_price_list() -> None:
    pricing = Pricing(
        version="qwen-2026-09", currency="CNY", prompt_per_1k=0.02, completion_per_1k=0.06
    )
    assert pricing.estimate(1000, 500) == pytest.approx(0.05)
    assert pricing.version in str(pricing)


def test_a_partial_usage_total_is_labelled_as_partial() -> None:
    """A total built from some calls is not a smaller total, it is an unknown one."""
    from backend.app.infrastructure.chat_completion import UsageRecord

    usage = UsageRecord()
    usage.record({"usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    usage.record({"choices": [{"message": {"content": "{}"}}]})
    assert usage.calls == 2
    assert usage.calls_with_usage == 1
    assert usage.total_tokens == 12
    assert not usage.complete


def test_a_gateway_reports_its_own_usage() -> None:
    from backend.app.infrastructure.chat_completion import UsageRecord

    class _Reporting:
        version = "reporting-v1"
        usage = UsageRecord()

        async def extract_profile(
            self, *, full_text: str, blocks: Sequence[ParsedBlock]
        ) -> CandidateProfileDraft:
            return CandidateProfileDraft(skills=[SkillClaim(name="python")])

    gateway = _Reporting()
    gateway.usage.record({"usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    run = asyncio.run(
        run_corpus(
            corpus=BUILTIN_CORPUS,
            cases=BUILTIN_CORPUS.cases[:1],
            gateway=gateway,
            embedding_gateway=FakeEmbeddingGateway(dimension=64),
            config=_CONFIG,
        )
    )
    assert run.usage is not None
    assert run.usage.total_tokens == 10
    assert run.source is PredictionSource.LIVE_MODEL


def test_an_unranked_candidate_is_recorded_as_unranked() -> None:
    """``None`` must stay distinguishable from "ranked last"."""
    run = _run_builtin()
    assert all(case.snapshot_order is None or case.snapshot_order >= 1 for case in run.cases)
    ranked = [case for case in run.cases if case.snapshot_order is not None]
    assert ranked, "the widened cut admits the whole corpus"
    for family in {case.job_family for case in ranked}:
        orders = [case.snapshot_order for case in ranked if case.job_family == family]
        assert len(set(orders)) == len(orders), "one rank per candidate inside a posting"


def test_the_run_records_the_versions_a_reader_needs_to_reproduce_it() -> None:
    run = _run_builtin()
    assert run.corpus_name and run.corpus_version
    assert run.content_hash == content_hash(BUILTIN_CORPUS)
    assert run.prompt_version and run.rule_version
    assert run.gateway_version and run.embedding_version
    assert run.pool_size == len(BUILTIN_CORPUS.cases)
    assert run.elapsed_seconds >= 0.0


def test_an_unexpected_profile_id_would_not_be_silently_ranked() -> None:
    """Every prediction carries its own profile id, so a lookup cannot drift."""
    run = _run_builtin()
    known = {case.profile_id for case in BUILTIN_CORPUS.cases}
    for case in run.cases:
        assert case.profile_id in known
        assert case.profile_id in case.ranked_profile_ids


def test_profile_ids_are_stable_between_runs() -> None:
    first = {case.case_id: case.profile_id for case in _run_builtin().cases}
    second = {case.case_id: case.profile_id for case in _run_builtin().cases}
    assert first == second


def test_uuid_identity_is_not_confused_with_an_index() -> None:
    """Guards the earlier truncation bug: siblings in other families must differ."""
    families = {case.job_family for case in BUILTIN_CORPUS.cases}
    by_archetype: dict[str, set[UUID]] = {}
    for case in BUILTIN_CORPUS.cases:
        by_archetype.setdefault(case.archetype, set()).add(case.profile_id)
    assert all(len(ids) == len(families) for ids in by_archetype.values())
