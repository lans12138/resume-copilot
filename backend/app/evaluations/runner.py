"""Run the corpus through the real pipeline and record what came out (PORT-004).

The corpus declares inputs and answers; this module produces the third thing an
evaluation needs — predictions that were actually *generated*, from the raw text,
by the code under test. Nothing here reimplements a stage: extraction goes through
the configured ``ModelGateway``, recall through ``RetrievalService`` and RRF fusion,
hard rules through ``evaluate_hard_rules``, and the report through
``build_candidate_report``. A prediction that this module invents would be a
prediction the system never made.

**Every run is labelled by where its predictions came from.** ``PredictionSource``
is not decoration: a heuristic stand-in, a replayed recording and a live model call
produce numbers that are not comparable, and averaging them would be the same
category error the old suites made by copying predictions into labels. The report
keeps them apart and so does the gate.

**Setup failures fail the run.** An empty corpus, no candidate profiles, or a
recording that does not cover every input raises :class:`EvaluationSetupError`
before any metric exists. A missing fixture is an incomplete evaluation, not a
low score, and a run that cannot measure must not be able to conclude "passed".

**Per-case failures are recorded, not swallowed.** One case whose extraction call
fails is a failure count and a reason code, so the report can say how many cases it
actually measured. It is never silently dropped, which would inflate every rate.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID, uuid5

from backend.app.candidates.models import EvidenceChunk
from backend.app.candidates.schemas import CandidateProfileDraft
from backend.app.documents.parsers import DocxParagraphLocator, ParsedBlock
from backend.app.evaluations.corpus import CorpusCase, EvaluationCorpus, Split
from backend.app.infrastructure.chat_completion import UsageRecord
from backend.app.infrastructure.embedding import EmbeddingGateway
from backend.app.infrastructure.model_gateway import FakeModelGateway, ModelGateway
from backend.app.infrastructure.prompts import EXTRACTION_PROMPT_VERSION
from backend.app.infrastructure.recording import RecordedModelGateway
from backend.app.match_run.models import MatchRun, MatchRunCandidate
from backend.app.reports.models import ClaimView
from backend.app.reports.service import build_candidate_report
from backend.app.retrieval.fusion import fuse
from backend.app.retrieval.hard_rules import evaluate_hard_rules, hard_rule_snapshot
from backend.app.retrieval.models import (
    ChunkVector,
    HardRuleBundle,
    JobQuery,
    ReadyProfile,
    RetrievalConfig,
    RetrievalQuery,
)
from backend.app.retrieval.service import RetrievalService

#: Application ids are derived from the case id for the same reason profile ids
#: are: a run is reproducible and a report traces back to a case without a side
#: table.
_APPLICATION_NAMESPACE = UUID("0a000000-0000-4000-8000-000000000000")

#: Documents are synthetic; one document per case is enough to satisfy the
#: chunk↔document composite FK without inventing a parsing step.
_DOCUMENT_NAMESPACE = UUID("0d000000-0000-4000-8000-000000000000")

EMPTY_CORPUS = "EVALUATION_CORPUS_EMPTY"
NO_CANDIDATE_PROFILES = "EVALUATION_NO_CANDIDATE_PROFILES"
INCOMPLETE_RECORDING = "EVALUATION_RECORDING_INCOMPLETE"
EXTRACTION_FAILED = "EVALUATION_EXTRACTION_FAILED"


class EvaluationSetupError(RuntimeError):
    """The run cannot measure anything, so it must not conclude anything.

    Carries a stable ``code`` so the caller can distinguish "the fixture is
    missing" from "the corpus is empty" without parsing a message.
    """

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class PredictionSource(StrEnum):
    """Where a run's predictions came from. Never merged, never averaged.

    The first three are the classes the roadmap names; ``FAKE_MODEL`` is the
    honest label for the deterministic stand-in, which is neither a recorded
    response nor a live one and must never be presented as either (BR-010).
    """

    #: Built-in predictions scored against built-in labels — the existing suites.
    SCORER_FIXTURE = "SCORER_FIXTURE"
    #: Raw inputs through the heuristic stand-in; CI only, visibly not a model.
    FAKE_MODEL = "FAKE_MODEL"
    #: Real responses captured earlier and replayed through the real parser.
    RECORDED_REPLAY = "RECORDED_REPLAY"
    #: A live real-model call in this run.
    LIVE_MODEL = "LIVE_MODEL"


def classify_gateway(gateway: ModelGateway) -> PredictionSource:
    """Name the provenance of a gateway's output.

    Checked in this order because the replay adapter is what decides provenance:
    a recording is evidence that the real contract was exercised, and a run that
    used one is not the same as a run that called the model today.
    """
    if isinstance(gateway, RecordedModelGateway):
        return PredictionSource.RECORDED_REPLAY
    if isinstance(gateway, FakeModelGateway):
        return PredictionSource.FAKE_MODEL
    return PredictionSource.LIVE_MODEL


# --------------------------------------------------------------------------- #
# The corpus as a retrieval corpus
# --------------------------------------------------------------------------- #


def document_id(case_id: str) -> UUID:
    return uuid5(_DOCUMENT_NAMESPACE, case_id)


def application_id(case_id: str) -> UUID:
    return uuid5(_APPLICATION_NAMESPACE, case_id)


def chunks_for(case: CorpusCase) -> tuple[EvidenceChunk, ...]:
    """One evidence chunk per resume section, in document order.

    Section-per-chunk rather than paragraph-per-chunk because the corpus's
    ``CROSS_SEGMENT_EVIDENCE`` case only means something if the values really do
    live in separate chunks — and because a chunk is the unit the §9.4 verifier
    and the evidence binder both address.
    """
    document = document_id(case.case_id)
    chunks: list[EvidenceChunk] = []
    offset = 0
    for index, (section, text) in enumerate(case.sections):
        start = offset
        offset += len(text) + 1  # the "\n" that ``resume_text`` joins with
        chunks.append(
            EvidenceChunk(
                id=uuid5(document, f"{index}"),
                document_id=document,
                candidate_profile_id=case.profile_id,
                chunk_index=index,
                section_type=section,
                locator_json={
                    "paragraph_index": index,
                    "char_start": start,
                    "char_end": start + len(text),
                },
                text=text,
                text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(chunks)


def blocks_for(case: CorpusCase) -> tuple[ParsedBlock, ...]:
    """The parsed blocks a real extraction call would receive alongside the text."""
    blocks: list[ParsedBlock] = []
    offset = 0
    for index, (_section, text) in enumerate(case.sections):
        start = offset
        offset += len(text) + 1
        blocks.append(
            ParsedBlock(
                block_index=index,
                text=text,
                locator=DocxParagraphLocator(
                    paragraph_index=index,
                    char_start=start,
                    char_end=start + len(text),
                ),
            )
        )
    return tuple(blocks)


@dataclass(frozen=True, slots=True)
class CorpusRepository:
    """A ``RetrievalRepository`` over the corpus.

    Implements the same three-method port the SQL adapter does, so recall runs
    through ``RetrievalService`` unchanged. Ranking over an in-memory corpus and
    ranking over Postgres then differ only in where the rows came from.
    """

    profiles: tuple[ReadyProfile, ...]
    jobs: dict[UUID, JobQuery]
    vectors: tuple[ChunkVector, ...]

    async def list_ready_profiles(self) -> list[ReadyProfile]:
        return list(self.profiles)

    async def list_chunk_vectors(self, profile_ids: list[UUID]) -> list[ChunkVector]:
        wanted = set(profile_ids)
        return [item for item in self.vectors if item.candidate_profile_id in wanted]

    async def get_job_query(self, job_version_id: UUID) -> JobQuery:
        try:
            return self.jobs[job_version_id]
        except KeyError as error:
            raise ValueError(f"job version not found: {job_version_id}") from error


async def build_repository(
    cases: Sequence[CorpusCase], gateway: EmbeddingGateway
) -> CorpusRepository:
    """Project the cases into a retrieval corpus, embedding every chunk.

    One embedding call for the whole corpus rather than one per case: the number
    of model calls is itself something the report counts, and a loop would make it
    an implementation detail of this function.
    """
    profiles: list[ReadyProfile] = []
    jobs: dict[UUID, JobQuery] = {}
    chunk_texts: list[str] = []
    owners: list[UUID] = []
    for case in cases:
        profiles.append(case.ready_profile())
        jobs[case.job.job_version_id] = case.job
        for chunk in chunks_for(case):
            chunk_texts.append(chunk.text)
            owners.append(chunk.candidate_profile_id)

    embeddings = await gateway.embed(chunk_texts) if chunk_texts else []
    vectors = tuple(
        ChunkVector(candidate_profile_id=owner, embedding=list(vector))
        for owner, vector in zip(owners, embeddings, strict=True)
    )
    return CorpusRepository(profiles=tuple(profiles), jobs=jobs, vectors=vectors)


# --------------------------------------------------------------------------- #
# One case's predictions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CasePrediction:
    """What the pipeline produced for one case, or why it produced nothing."""

    case_id: str
    profile_id: UUID
    job_family: str
    split: Split
    relevant: bool
    draft: CandidateProfileDraft | None
    bundle: HardRuleBundle | None
    claims: tuple[ClaimView, ...]
    ranked_profile_ids: tuple[UUID, ...]
    snapshot_order: int | None
    error_code: str | None = None

    @property
    def measured(self) -> bool:
        """False when the case contributed no prediction to any metric."""
        return self.error_code is None


@dataclass(frozen=True, slots=True)
class CorpusRun:
    """The full record of one evaluation run: predictions plus their provenance."""

    source: PredictionSource
    gateway_version: str
    embedding_version: str
    model_name: str
    corpus_name: str
    corpus_version: str
    content_hash: str
    prompt_version: str
    rule_version: str
    retrieval_top_k: int
    pool_size: int
    cases: tuple[CasePrediction, ...]
    elapsed_seconds: float
    usage: UsageRecord | None

    @property
    def failures(self) -> tuple[CasePrediction, ...]:
        return tuple(case for case in self.cases if not case.measured)

    @property
    def measured_cases(self) -> tuple[CasePrediction, ...]:
        return tuple(case for case in self.cases if case.measured)

    def of_split(self, which: Split) -> tuple[CasePrediction, ...]:
        return tuple(case for case in self.cases if case.split is which)


def content_hash(corpus: EvaluationCorpus) -> str:
    """A hash over the corpus's inputs *and* gold, not over a description of them."""
    payload = json.dumps(corpus.content(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def gateway_usage(gateway: ModelGateway) -> UsageRecord | None:
    """The gateway's token counter, when it has one.

    ``None`` is a real answer: the deterministic stand-in has no counter, and a
    zero would be read as "this run was free" rather than "this run cannot be
    priced". The report distinguishes the two.
    """
    record = getattr(gateway, "usage", None)
    return record if isinstance(record, UsageRecord) else None


def _match_run(*, case: CorpusCase, config: RetrievalConfig, source: PredictionSource) -> MatchRun:
    """An in-memory MatchRun carrying the frozen config a report snapshots."""
    return MatchRun(
        run_id=uuid5(_APPLICATION_NAMESPACE, f"run:{case.case_id}"),
        job_id=uuid5(_APPLICATION_NAMESPACE, f"job:{case.job_family}"),
        job_version_id=case.job.job_version_id,
        retrieval_config_json={
            "structured_weight": config.structured_weight,
            "keyword_weight": config.keyword_weight,
            "vector_weight": config.vector_weight,
            "rrf_k": config.rrf_k,
            "top_k": config.top_k,
        },
        model_config_json={"source": source.value, "prompt_version": EXTRACTION_PROMPT_VERSION},
        prompt_version=EXTRACTION_PROMPT_VERSION,
        rule_version=config.rule_version,
    )


def _candidate(
    *, case: CorpusCase, order: int, rrf_score: float, bundle: HardRuleBundle | None
) -> MatchRunCandidate:
    return MatchRunCandidate(
        id=uuid5(_APPLICATION_NAMESPACE, f"candidate:{case.case_id}"),
        run_id=uuid5(_APPLICATION_NAMESPACE, f"run:{case.case_id}"),
        candidate_profile_id=case.profile_id,
        application_id=application_id(case.case_id),
        snapshot_order=order,
        rrf_score=rrf_score,
        hard_rule_result_json=hard_rule_snapshot(bundle),
    )


async def _predict_case(
    *,
    case: CorpusCase,
    gateway: ModelGateway,
    service: RetrievalService,
    config: RetrievalConfig,
    source: PredictionSource,
) -> CasePrediction:
    """Run one case end to end, converting a model failure into a reason code."""
    try:
        draft = await gateway.extract_profile(
            full_text=case.resume_text, blocks=list(blocks_for(case))
        )
    except Exception as error:  # noqa: BLE001 - one case must not abort the run
        # The failure is recorded rather than raised: a run that measured 47 of 48
        # cases can still be reported, as long as the report says so. What must not
        # happen is the case silently disappearing, which would inflate every rate.
        return CasePrediction(
            case_id=case.case_id,
            profile_id=case.profile_id,
            job_family=case.job_family,
            split=case.split,
            relevant=case.relevant,
            draft=None,
            bundle=None,
            claims=(),
            ranked_profile_ids=(),
            snapshot_order=None,
            error_code=f"{EXTRACTION_FAILED}:{type(error).__name__}",
        )

    context = await service.run_recall_full(
        RetrievalQuery(
            job_version_id=case.job.job_version_id,
            top_k=config.top_k,
            rule_version=config.rule_version,
            channels=config.channels,
        )
    )
    fused = fuse(context.bundle, config)
    ranked = tuple(candidate.candidate_profile_id for candidate in fused)

    profile = {item.profile_id: item for item in context.profiles}[case.profile_id]
    bundle = evaluate_hard_rules(context.job, profile)

    order = ranked.index(case.profile_id) + 1 if case.profile_id in ranked else None
    rrf_score = next(
        (
            candidate.rrf_score
            for candidate in fused
            if candidate.candidate_profile_id == case.profile_id
        ),
        0.0,
    )
    view = build_candidate_report(
        candidate=_candidate(
            case=case,
            # A candidate outside the fused cut still needs a positive order for the
            # report's rank-derived score. It is placed just past the cut, which is
            # where it would have been ranked, rather than given a rank it did not
            # earn. ``snapshot_order`` on the prediction keeps the None, so a metric
            # can tell "ranked last" from "not ranked at all".
            order=order if order is not None else len(ranked) + 1,
            rrf_score=rrf_score,
            bundle=bundle,
        ),
        chunks=list(chunks_for(case)),
        match_run=_match_run(case=case, config=config, source=source),
        application_id=application_id(case.case_id),
    )
    return CasePrediction(
        case_id=case.case_id,
        profile_id=case.profile_id,
        job_family=case.job_family,
        split=case.split,
        relevant=case.relevant,
        draft=draft,
        bundle=bundle,
        claims=tuple(view.claims),
        ranked_profile_ids=ranked,
        snapshot_order=order,
    )


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


def _widen_to_pool(config: RetrievalConfig, pool_size: int) -> RetrievalConfig:
    """Cut the fused ranking at the pool size instead of the production Top-K.

    RRF scores are computed over the whole ranking and only then truncated, so
    widening the cut changes which candidates a *report* would list, not how they
    are scored. It is done here because Recall@K is undefined for K beyond the cut,
    and the run records both numbers so the difference is visible rather than
    implied.
    """
    return replace(config, top_k=max(config.top_k, pool_size))


async def run_corpus(
    *,
    corpus: EvaluationCorpus,
    cases: Sequence[CorpusCase],
    gateway: ModelGateway,
    embedding_gateway: EmbeddingGateway,
    config: RetrievalConfig,
    model_name: str = "",
) -> CorpusRun:
    """Run every case and return the predictions with their provenance.

    Raises :class:`EvaluationSetupError` when the run cannot measure: no cases, no
    profiles, or a replay gateway that does not cover every input. Those are
    conditions under which any metric would be meaningless, and a run that cannot
    measure must not be able to report a pass.
    """
    if not cases:
        raise EvaluationSetupError(EMPTY_CORPUS, "评测集为空，无法产生任何指标")
    if isinstance(gateway, RecordedModelGateway):
        missing = [case.case_id for case in cases if not gateway.has_call_for(case.resume_text)]
        if missing:
            raise EvaluationSetupError(
                INCOMPLETE_RECORDING,
                "录制文件未覆盖全部用例，缺失 "
                f"{len(missing)} 例（例如 {missing[0]}）；请重新录制后再评测",
            )

    repository = await build_repository(cases, embedding_gateway)
    if not repository.profiles:
        raise EvaluationSetupError(NO_CANDIDATE_PROFILES, "检索语料为空，无法产生排序")

    effective = _widen_to_pool(config, len(repository.profiles))
    service = RetrievalService(repository, embedding_gateway)

    started = time.perf_counter()
    predictions = [
        await _predict_case(
            case=case,
            gateway=gateway,
            service=service,
            config=effective,
            source=classify_gateway(gateway),
        )
        for case in cases
    ]
    elapsed = time.perf_counter() - started

    return CorpusRun(
        source=classify_gateway(gateway),
        gateway_version=gateway.version,
        embedding_version=embedding_gateway.version,
        model_name=model_name,
        corpus_name=corpus.name,
        corpus_version=corpus.version,
        content_hash=content_hash(corpus),
        prompt_version=EXTRACTION_PROMPT_VERSION,
        rule_version=config.rule_version,
        retrieval_top_k=config.top_k,
        pool_size=len(repository.profiles),
        cases=tuple(predictions),
        elapsed_seconds=elapsed,
        usage=gateway_usage(gateway),
    )


__all__ = [
    "EMPTY_CORPUS",
    "EXTRACTION_FAILED",
    "INCOMPLETE_RECORDING",
    "NO_CANDIDATE_PROFILES",
    "CasePrediction",
    "CorpusRepository",
    "CorpusRun",
    "EvaluationSetupError",
    "PredictionSource",
    "application_id",
    "blocks_for",
    "build_repository",
    "chunks_for",
    "classify_gateway",
    "content_hash",
    "document_id",
    "gateway_usage",
    "run_corpus",
]
