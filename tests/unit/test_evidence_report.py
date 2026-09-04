"""Hermetic tests for evidence-backed reports and semantic evaluation (IMP-020).

No database, no model calls: in-memory repositories and hand-built
``EvidenceChunk``/``MatchRunCandidate`` rows drive every scenario. The gate
checks five things:

* §9.4 reference verification catches every illegal class (chunk missing,
  ownership mismatch, bad slice range, text mismatch) and accepts a legal one.
* a HIGH-impact claim that is not SUPPORTED is rewritten with the
  "insufficient evidence" template (§4.5).
* ``ReportService`` persists one report per COMPLETED candidate, drops illegal
  references (counted, never saved), and skips FAILED candidates.
* the built-in semantic evaluation clears the gate (SUPPORTED precision and
  three-class macro-F1 thresholds).
* a MatchRun that completes with a ``ReportService`` persists queryable reports.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from backend.app.agent.models import AgentRun, RunStatus, RunType
from backend.app.agent.repository import InMemoryAgentRunRepository
from backend.app.candidates.models import EvidenceChunk
from backend.app.evaluations.semantic import (
    build_semantic_report,
    evaluate_support_labels,
    run_builtin_semantic_evaluation,
)
from backend.app.match_run.models import MatchRun, MatchRunCandidate, ProcessingStatus
from backend.app.match_run.repository import (
    InMemoryMatchRunCandidateRepository,
    InMemoryMatchRunRepository,
)
from backend.app.match_run.service import MatchRunService
from backend.app.reports.models import ClaimEvidence, ImpactLevel, SupportLevel
from backend.app.reports.repository import InMemoryReportRepository
from backend.app.reports.service import (
    EvidenceProvider,
    ReportBuildResult,
    ReportService,
    build_candidate_report,
)
from backend.app.reports.validation import (
    INSUFFICIENT_TEMPLATE,
    apply_high_impact_guard,
    verify_evidence_reference,
)
from backend.app.retrieval.models import (
    FusedCandidate,
    HardRuleBundle,
    HardRuleOutcome,
    RankingSnapshot,
    RetrievalConfig,
)


def _chunk(profile_id: UUID, *, text: str = "candidate has 5 years experience") -> EvidenceChunk:
    return EvidenceChunk(
        id=uuid4(),
        document_id=uuid4(),
        candidate_profile_id=profile_id,
        chunk_index=0,
        section_type="skill",
        locator_json={"page": 1},
        text=text,
        text_sha256="x" * 64,
    )


def _candidate(
    profile_id: UUID,
    order: int,
    overall: HardRuleOutcome,
    *,
    status: ProcessingStatus = ProcessingStatus.COMPLETED,
) -> MatchRunCandidate:
    return MatchRunCandidate(
        run_id=uuid4(),
        candidate_profile_id=profile_id,
        application_id=profile_id,
        snapshot_order=order,
        rrf_score=float(10 - order),
        processing_status=status,
        hard_rule_result_json={"overall": overall.value, "rules": []},
    )


def _agent_run(run_id: UUID) -> AgentRun:
    return AgentRun(
        id=run_id,
        thread_id="thread",
        run_type=RunType.MATCH,
        status=RunStatus.COMPLETED,
        attempt=1,
        next_event_sequence=0,
        version=1,
        config_snapshot_json={},
    )


def _match_run(run_id: UUID) -> MatchRun:
    return MatchRun(
        run_id=run_id,
        job_id=uuid4(),
        job_version_id=uuid4(),
        retrieval_config_json={},
        model_config_json={"model": "fake"},
        prompt_version="v1",
        rule_version="v1",
    )


# --------------------------------------------------------------------------- #
# §9.4 reference verification
# --------------------------------------------------------------------------- #


def test_verify_legal_reference() -> None:
    profile = uuid4()
    chunk = _chunk(profile)
    evidence = ClaimEvidence(
        id=uuid4(),
        claim_id=uuid4(),
        evidence_chunk_id=chunk.id,
        quote_text=chunk.text,
        quote_start=0,
        quote_end=len(chunk.text),
    )
    verdict = verify_evidence_reference(
        evidence=evidence, chunk=chunk, candidate_profile_id=profile
    )
    assert verdict.legal
    assert verdict.reason == ""


def test_verify_rejects_missing_chunk() -> None:
    evidence = ClaimEvidence(
        id=uuid4(),
        claim_id=uuid4(),
        evidence_chunk_id=uuid4(),
        quote_text="x",
        quote_start=0,
        quote_end=1,
    )
    verdict = verify_evidence_reference(
        evidence=evidence, chunk=None, candidate_profile_id=uuid4()
    )
    assert not verdict.legal
    assert verdict.reason == "chunk_not_found"


def test_verify_rejects_ownership_mismatch() -> None:
    profile = uuid4()
    chunk = _chunk(uuid4())  # belongs to a different candidate
    evidence = ClaimEvidence(
        id=uuid4(),
        claim_id=uuid4(),
        evidence_chunk_id=chunk.id,
        quote_text=chunk.text,
        quote_start=0,
        quote_end=len(chunk.text),
    )
    verdict = verify_evidence_reference(
        evidence=evidence, chunk=chunk, candidate_profile_id=profile
    )
    assert not verdict.legal
    assert verdict.reason == "ownership_mismatch"


def test_verify_rejects_bad_range() -> None:
    profile = uuid4()
    chunk = _chunk(profile, text="abc")
    evidence = ClaimEvidence(
        id=uuid4(),
        claim_id=uuid4(),
        evidence_chunk_id=chunk.id,
        quote_text="abc",
        quote_start=0,
        quote_end=99,  # beyond chunk length
    )
    verdict = verify_evidence_reference(
        evidence=evidence, chunk=chunk, candidate_profile_id=profile
    )
    assert not verdict.legal
    assert verdict.reason == "quote_range_invalid"


def test_verify_rejects_text_mismatch() -> None:
    profile = uuid4()
    chunk = _chunk(profile, text="abc")
    evidence = ClaimEvidence(
        id=uuid4(),
        claim_id=uuid4(),
        evidence_chunk_id=chunk.id,
        quote_text="XYZ",  # does not match the slice
        quote_start=0,
        quote_end=3,
    )
    verdict = verify_evidence_reference(
        evidence=evidence, chunk=chunk, candidate_profile_id=profile
    )
    assert not verdict.legal
    assert verdict.reason == "quote_text_mismatch"


# --------------------------------------------------------------------------- #
# §4.5 high-impact downgrade
# --------------------------------------------------------------------------- #


def test_high_impact_guard_rewrites_unsupported() -> None:
    text, support = apply_high_impact_guard(
        impact_level=ImpactLevel.HIGH,
        support_level=SupportLevel.INSUFFICIENT,
        claim_text="候选人明确不满足硬性条件",
    )
    assert text == INSUFFICIENT_TEMPLATE
    assert support == SupportLevel.INSUFFICIENT


def test_high_impact_guard_keeps_supported() -> None:
    text, support = apply_high_impact_guard(
        impact_level=ImpactLevel.HIGH,
        support_level=SupportLevel.SUPPORTED,
        claim_text="候选人满足硬性条件",
    )
    assert text == "候选人满足硬性条件"
    assert support == SupportLevel.SUPPORTED


# --------------------------------------------------------------------------- #
# report builder
# --------------------------------------------------------------------------- #


def test_build_pass_candidate_cites_evidence() -> None:
    profile = uuid4()
    chunk = _chunk(profile)
    candidate = _candidate(profile, 1, HardRuleOutcome.PASS)
    view = build_candidate_report(
        candidate=candidate,
        chunks=[chunk],
        match_run=_match_run(uuid4()),
        application_id=profile,
    )
    assert view.report.recommendation.value in {"STRONG_MATCH", "MATCH"}
    overall = view.claims[0]
    assert overall.claim.support_level == SupportLevel.SUPPORTED
    assert overall.claim.impact_level == ImpactLevel.MEDIUM
    # A SUPPORTED claim pins the verbatim chunk.
    assert len(overall.evidences) == 1
    assert overall.evidences[0].evidence_chunk_id == chunk.id


def test_build_fail_candidate_downgraded_to_insufficient() -> None:
    profile = uuid4()
    chunk = _chunk(profile)
    candidate = _candidate(profile, 2, HardRuleOutcome.FAIL)
    view = build_candidate_report(
        candidate=candidate,
        chunks=[chunk],
        match_run=_match_run(uuid4()),
        application_id=profile,
    )
    overall = view.claims[0]
    assert overall.claim.impact_level == ImpactLevel.HIGH
    assert overall.claim.support_level == SupportLevel.INSUFFICIENT
    assert overall.claim.claim_text == INSUFFICIENT_TEMPLATE
    # No verbatim assertion is pinned for an INSUFFICIENT claim.
    assert overall.evidences == []


def test_build_without_chunks_emits_no_deterministic_evidence() -> None:
    profile = uuid4()
    candidate = _candidate(profile, 1, HardRuleOutcome.UNKNOWN)
    view = build_candidate_report(
        candidate=candidate,
        chunks=[],
        match_run=_match_run(uuid4()),
        application_id=profile,
    )
    assert all(claim.evidences == [] for claim in view.claims)
    assert all(c.claim.support_level == SupportLevel.INSUFFICIENT for c in view.claims)


# --------------------------------------------------------------------------- #
# ReportService persistence + illegal reference counting
# --------------------------------------------------------------------------- #


class _ChunkProvider:
    def __init__(self, by_profile: dict[UUID, list[EvidenceChunk]]) -> None:
        self._by_profile = by_profile

    async def list_chunks(self, candidate_profile_id: UUID) -> list[EvidenceChunk]:
        return self._by_profile.get(candidate_profile_id, [])


def _generate(
    candidates: list[MatchRunCandidate],
    by_profile: dict[UUID, list[EvidenceChunk]],
) -> tuple[InMemoryReportRepository, ReportBuildResult]:
    repo = InMemoryReportRepository()
    service = ReportService(repo)
    run = _agent_run(uuid4())
    match_run = _match_run(run.id)
    result = asyncio.run(
        service.generate_for_run(
            run=run,
            match_run=match_run,
            candidates=candidates,
            evidence=_ChunkProvider(by_profile),
        )
    )
    return repo, result


def test_generate_writes_one_report_per_completed_candidate() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    chunks = {
        a: [_chunk(a)],
        b: [_chunk(b)],
        c: [_chunk(c)],
    }
    candidates = [
        _candidate(a, 1, HardRuleOutcome.PASS),
        _candidate(b, 2, HardRuleOutcome.UNKNOWN),
        _candidate(c, 3, HardRuleOutcome.FAIL),
    ]
    repo, result = _generate(candidates, chunks)
    assert result.illegal_reference_count == 0
    assert result.reports_written == 3
    views = asyncio.run(repo.list_by_run(_run_id_of(repo)))
    assert len(views) == 3


def test_generate_skips_failed_candidates() -> None:
    good = uuid4()
    bad = uuid4()
    chunks = {good: [_chunk(good)], bad: [_chunk(bad)]}
    candidates = [
        _candidate(good, 1, HardRuleOutcome.PASS),
        _candidate(bad, 2, HardRuleOutcome.PASS, status=ProcessingStatus.FAILED),
    ]
    repo, result = _generate(candidates, chunks)
    assert result.reports_written == 1
    assert result.illegal_reference_count == 0


def test_generate_counts_and_drops_illegal_reference() -> None:
    profile = uuid4()
    other = uuid4()
    # The provider returns a chunk that belongs to `other`, not `profile`.
    chunks = {profile: [_chunk(other, text="foreign evidence")]}
    candidates = [_candidate(profile, 1, HardRuleOutcome.PASS)]
    repo, result = _generate(candidates, chunks)
    # The illegal reference is counted and NOT persisted; the report still lands.
    assert result.illegal_reference_count == 1
    assert result.reports_written == 1
    views = asyncio.run(repo.list_by_run(_run_id_of(repo)))
    assert len(views) == 1
    assert all(len(claim.evidences) == 0 for claim in views[0].claims)


def _run_id_of(repo: InMemoryReportRepository) -> UUID:
    # The repository stores reports keyed by id; read back the single run id.
    return next(iter(repo._reports.values())).run_id  # noqa: SLF001


# --------------------------------------------------------------------------- #
# semantic evaluation gate
# --------------------------------------------------------------------------- #


def test_evaluate_support_labels_computes_metrics() -> None:
    predicted = [SupportLevel.SUPPORTED, SupportLevel.INSUFFICIENT, SupportLevel.PARTIAL]
    gold = [SupportLevel.SUPPORTED, SupportLevel.INSUFFICIENT, SupportLevel.PARTIAL]
    metrics = evaluate_support_labels(predicted, gold)
    assert metrics.supported_precision == 1.0
    assert metrics.macro_f1 == 1.0


def test_evaluate_support_labels_flags_false_positive() -> None:
    predicted = [SupportLevel.SUPPORTED, SupportLevel.INSUFFICIENT]
    gold = [SupportLevel.INSUFFICIENT, SupportLevel.INSUFFICIENT]
    metrics = evaluate_support_labels(predicted, gold)
    # One asserted SUPPORTED that is wrong -> precision 0.
    assert metrics.supported_precision == 0.0


def test_builtin_semantic_evaluation_passes_gate() -> None:
    report = run_builtin_semantic_evaluation(illegal_reference_count=0)
    assert report.passed
    assert report.supported_precision_pass
    assert report.macro_f1_pass
    assert report.illegal_reference_pass


def test_semantic_report_fails_on_illegal_reference() -> None:
    report = build_semantic_report(
        cases=[([SupportLevel.SUPPORTED], [SupportLevel.SUPPORTED])],
        illegal_reference_count=1,
    )
    assert not report.passed
    assert not report.illegal_reference_pass


# --------------------------------------------------------------------------- #
# MatchRun integration: a completed run persists queryable reports (Gate G4)
# --------------------------------------------------------------------------- #


JOB_VERSION_ID = uuid4()
CONFIG = RetrievalConfig(
    structured_weight=1.0,
    keyword_weight=1.0,
    vector_weight=1.0,
    rrf_k=60,
    top_k=10,
    rule_version="v1",
)


def _snapshot(fused: list[FusedCandidate]) -> RankingSnapshot:
    return RankingSnapshot(job_version_id=JOB_VERSION_ID, config=CONFIG, fused=fused)


class _Rankings:
    def __init__(self, snap: RankingSnapshot) -> None:
        self._snap = snap

    async def get_snapshot(self, *, job_version_id: UUID) -> RankingSnapshot:
        return self._snap


def test_match_run_persists_reports_on_completion() -> None:
    a, b = uuid4(), uuid4()
    chunks = {a: [_chunk(a)], b: [_chunk(b)]}
    fused = [
        FusedCandidate(
            candidate_profile_id=a,
            snapshot_order=1,
            rrf_score=9.0,
            hard_rule=HardRuleBundle(rules=[], overall=HardRuleOutcome.PASS),
        ),
        FusedCandidate(
            candidate_profile_id=b,
            snapshot_order=2,
            rrf_score=8.0,
            hard_rule=HardRuleBundle(rules=[], overall=HardRuleOutcome.UNKNOWN),
        ),
    ]
    application_ids = {a: a, b: b}

    run_repository = InMemoryAgentRunRepository()
    match_run_repository = InMemoryMatchRunRepository()
    candidate_repository = InMemoryMatchRunCandidateRepository()
    report_repository = InMemoryReportRepository()
    report_service = ReportService(report_repository)
    evidence_provider: EvidenceProvider = _ChunkProvider(chunks)

    service = MatchRunService(
        run_repository=run_repository,
        match_run_repository=match_run_repository,
        candidate_repository=candidate_repository,
        rankings=_Rankings(_snapshot(fused)),
        concurrency=4,
    )
    run, match_run = asyncio.run(
        service.create_match_run(
            job_id=uuid4(),
            job_version_id=JOB_VERSION_ID,
            actor_id=uuid4(),
            retrieval_config={"top_k": 10},
            model_config={"model": "fake"},
            prompt_version="v1",
            rule_version="v1",
            application_ids=application_ids,
        )
    )
    asyncio.run(
        service.execute_match_run(
            run=run,
            match_run=match_run,
            application_ids=application_ids,
            report_service=report_service,
            evidence_provider=evidence_provider,
        )
    )
    views = asyncio.run(report_repository.list_by_run(run.id))
    assert len(views) == 2
    # The PASS candidate's report pins a verbatim evidence excerpt.
    pass_view = next(v for v in views if v.report.candidate_profile_id == a)
    assert any(len(claim.evidences) == 1 for claim in pass_view.claims)
    # The UNKNOWN candidate's report carries no illegal references.
    assert all(
        len(claim.evidences) == 0 or claim.claim.support_level == SupportLevel.SUPPORTED
        for v in views
        for claim in v.claims
    )
