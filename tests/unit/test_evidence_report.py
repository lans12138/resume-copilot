"""Hermetic tests for evidence-backed reports and semantic evaluation (IMP-020).

No database, no model calls: in-memory repositories and hand-built
``EvidenceChunk``/``MatchRunCandidate`` rows drive every scenario. The gate
checks six things:

* §9.4 reference verification catches every illegal class (chunk missing,
  ownership mismatch, superseded profile version, bad slice range, text
  mismatch) and accepts a legal one.
* every claim cites the excerpt that actually states its observed value, and
  never falls back to ``chunks[0]`` (PORT-002).
* a claim whose value is absent, contradicted, or unrecognised is downgraded
  instead of asserted.
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
from backend.app.reports.models import (
    ClaimEvidence,
    ClaimView,
    ImpactLevel,
    Recommendation,
    ReportView,
    SupportLevel,
)
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
    HardRuleId,
    HardRuleOutcome,
    HardRuleResult,
    RankingSnapshot,
    RetrievalConfig,
)


def _chunk(
    profile_id: UUID,
    *,
    text: str = "candidate has 5 years experience",
    index: int = 0,
    section: str = "skill",
) -> EvidenceChunk:
    return EvidenceChunk(
        id=uuid4(),
        document_id=uuid4(),
        candidate_profile_id=profile_id,
        chunk_index=index,
        section_type=section,
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


def _rule(
    rule_id: str,
    result: HardRuleOutcome,
    *,
    observed: object = None,
    required: object = None,
) -> dict[str, object]:
    return {
        "rule_id": rule_id,
        "result": result.value,
        "observed_value": observed,
        "required_value": required,
    }


def _candidate_with_rules(
    profile_id: UUID,
    order: int,
    overall: HardRuleOutcome,
    rules: list[dict[str, object]],
    *,
    status: ProcessingStatus = ProcessingStatus.COMPLETED,
) -> MatchRunCandidate:
    """A candidate whose snapshot carries per-rule detail — the shape the real
    ``hard_rule_evaluate`` node writes, and the one PORT-002 binds evidence from."""
    candidate = _candidate(profile_id, order, overall, status=status)
    candidate.hard_rule_result_json = {"overall": overall.value, "rules": rules}
    return candidate


# A resume whose first chunk is deliberately unrelated to every rule below.
_RESUME = (
    (0, "个人爱好：马拉松、摄影", "hobby"),
    (1, "教育背景：本科，计算机科学与技术", "education"),
    (2, "工作经历：5 年 Python 后端开发，熟悉 Docker", "experience"),
)


def _resume_chunks(profile_id: UUID) -> list[EvidenceChunk]:
    return [
        _chunk(profile_id, text=text, index=index, section=section)
        for index, text, section in _RESUME
    ]


def _passing_rules() -> list[dict[str, object]]:
    return [
        _rule("years_experience", HardRuleOutcome.PASS, observed=5.0, required=3.0),
        _rule("required_education", HardRuleOutcome.PASS, observed="本科", required="本科"),
        _rule("required_skills", HardRuleOutcome.PASS, observed=["python"], required=["python"]),
    ]


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


def _build(
    profile: UUID, candidate: MatchRunCandidate, chunks: list[EvidenceChunk]
) -> ReportView:
    return build_candidate_report(
        candidate=candidate,
        chunks=chunks,
        match_run=_match_run(uuid4()),
        application_id=profile,
    )


def _claims(view: ReportView) -> dict[str, ClaimView]:
    return {claim_view.claim.claim_type: claim_view for claim_view in view.claims}


def test_build_locates_each_rule_in_its_own_excerpt() -> None:
    """The core PORT-002 case: the first chunk is unrelated to every rule, and
    each rule still cites the excerpt that states *its own* observed value.

    Before this change the builder pinned ``chunks[0]`` — the hobby line — to all
    three claims. The reference was legal and owned, so §9.4 passed, and the
    report proved nothing.
    """
    profile = uuid4()
    chunks = _resume_chunks(profile)
    hobby, education, experience = chunks
    candidate = _candidate_with_rules(profile, 1, HardRuleOutcome.PASS, _passing_rules())

    view = _build(profile, candidate, chunks)
    claims = _claims(view)

    years = claims["hard_rule:years_experience"]
    assert years.claim.support_level == SupportLevel.SUPPORTED
    assert [e.evidence_chunk_id for e in years.evidences] == [experience.id]
    assert [e.quote_text for e in years.evidences] == ["5 年"]
    assert years.claim.confidence_note == "result=PASS; evidence=located"

    education_claim = claims["hard_rule:required_education"]
    assert education_claim.claim.support_level == SupportLevel.SUPPORTED
    assert [e.evidence_chunk_id for e in education_claim.evidences] == [education.id]
    assert [e.quote_text for e in education_claim.evidences] == ["本科"]

    skills = claims["hard_rule:required_skills"]
    assert skills.claim.support_level == SupportLevel.SUPPORTED
    assert [e.evidence_chunk_id for e in skills.evidences] == [experience.id]
    assert [e.quote_text for e in skills.evidences] == ["Python"]

    # No claim ever falls back to the unrelated first chunk.
    assert all(e.evidence_chunk_id != hobby.id for c in view.claims for e in c.evidences)


def test_build_aggregate_claim_unions_the_rule_evidence() -> None:
    """The aggregate claim may only cite what the individual rules established."""
    profile = uuid4()
    chunks = _resume_chunks(profile)
    _, education, experience = chunks
    candidate = _candidate_with_rules(profile, 1, HardRuleOutcome.PASS, _passing_rules())

    view = _build(profile, candidate, chunks)
    overall = view.claims[0]
    assert overall.claim.claim_type == "hard_rule_overall"
    assert overall.claim.support_level == SupportLevel.SUPPORTED
    assert {e.evidence_chunk_id for e in overall.evidences} == {education.id, experience.id}
    # Re-keyed to the aggregate claim, so the FK target is right.
    assert all(e.claim_id == overall.claim.id for e in overall.evidences)


def test_build_without_rule_detail_emits_no_citable_evidence() -> None:
    """An aggregate-only snapshot records no observed values, so nothing is citable.

    The verdict still stands (it came from confirmed profile fields), but a claim
    resting on a verdict nobody can read is PARTIAL — never SUPPORTED.
    """
    profile = uuid4()
    candidate = _candidate(profile, 1, HardRuleOutcome.PASS)
    view = _build(profile, candidate, _resume_chunks(profile))

    assert view.report.recommendation == Recommendation.STRONG_MATCH
    assert len(view.claims) == 1
    overall = view.claims[0]
    assert overall.claim.impact_level == ImpactLevel.MEDIUM
    assert overall.claim.support_level == SupportLevel.PARTIAL
    assert overall.evidences == []
    assert "未在候选人原文中定位到支持该结论的片段" in overall.claim.claim_text


def test_build_downgrades_when_the_observed_value_is_absent() -> None:
    """A PASS the resume never states is PARTIAL, with the reason spelled out."""
    profile = uuid4()
    rules = [
        _rule("years_experience", HardRuleOutcome.PASS, observed=9.0, required=3.0),
        _rule("required_education", HardRuleOutcome.PASS, observed="硕士", required="本科"),
    ]
    candidate = _candidate_with_rules(profile, 1, HardRuleOutcome.PASS, rules)
    view = _build(profile, candidate, _resume_chunks(profile))

    assert all(c.claim.support_level == SupportLevel.PARTIAL for c in view.claims)
    assert all(c.evidences == [] for c in view.claims)
    for claim_view in view.claims:
        assert "未在候选人原文中定位到" in claim_view.claim.claim_text
        # The reason is machine-readable too, so the UI need not parse Chinese.
        assert claim_view.claim.confidence_note is not None
        assert claim_view.claim.confidence_note.endswith("evidence=not_found")


def test_build_cites_every_chunk_that_states_the_value() -> None:
    """One conclusion may rest on several excerpts; each is cited."""
    profile = uuid4()
    chunks = [
        _chunk(profile, text="技能：Python、Docker", index=0, section="skill"),
        _chunk(profile, text="项目中使用 Python 重构服务", index=1, section="project"),
    ]
    rules = [
        _rule("required_skills", HardRuleOutcome.PASS, observed=["python"], required=["python"])
    ]
    candidate = _candidate_with_rules(profile, 1, HardRuleOutcome.PASS, rules)
    view = _build(profile, candidate, chunks)

    skills = _claims(view)["hard_rule:required_skills"]
    assert {e.evidence_chunk_id for e in skills.evidences} == {chunks[0].id, chunks[1].id}


def test_build_marks_a_missing_field_unknown_not_fail() -> None:
    """§8.4: a missing field is UNKNOWN, and the wording must say "cannot tell"."""
    profile = uuid4()
    rules = [
        _rule("required_education", HardRuleOutcome.UNKNOWN, observed=None, required="本科"),
    ]
    candidate = _candidate_with_rules(profile, 1, HardRuleOutcome.UNKNOWN, rules)
    view = _build(profile, candidate, _resume_chunks(profile))

    claim = _claims(view)["hard_rule:required_education"]
    assert claim.claim.support_level == SupportLevel.INSUFFICIENT
    assert claim.claim.claim_text.startswith("无法判定required_education")
    assert claim.evidences == []

    overall = view.claims[0]
    assert overall.claim.support_level == SupportLevel.INSUFFICIENT
    assert overall.claim.impact_level == ImpactLevel.MEDIUM  # UNKNOWN is not FAIL
    assert overall.claim.claim_text == "硬性条件证据不足，无法判定是否满足"
    assert overall.evidences == []


def test_build_downgrades_a_contradicting_fail_to_insufficient() -> None:
    """A FAIL the text contradicts (or never states) is HIGH-impact and unbacked."""
    profile = uuid4()
    rules = [_rule("years_experience", HardRuleOutcome.FAIL, observed=1.0, required=3.0)]
    candidate = _candidate_with_rules(profile, 1, HardRuleOutcome.FAIL, rules)
    view = _build(profile, candidate, _resume_chunks(profile))

    claim = _claims(view)["hard_rule:years_experience"]
    assert claim.claim.impact_level == ImpactLevel.HIGH
    assert claim.claim.support_level == SupportLevel.INSUFFICIENT
    assert claim.claim.claim_text == INSUFFICIENT_TEMPLATE
    assert claim.evidences == []


def test_build_keeps_a_supported_high_impact_fail() -> None:
    """The §4.5 guard rewrites *unbacked* high-impact claims, not all of them."""
    profile = uuid4()
    rules = [_rule("years_experience", HardRuleOutcome.FAIL, observed=5.0, required=8.0)]
    candidate = _candidate_with_rules(profile, 1, HardRuleOutcome.FAIL, rules)
    view = _build(profile, candidate, _resume_chunks(profile))

    claim = _claims(view)["hard_rule:years_experience"]
    assert claim.claim.impact_level == ImpactLevel.HIGH
    assert claim.claim.support_level == SupportLevel.SUPPORTED
    assert claim.claim.claim_text.startswith("不满足years_experience")
    assert [e.quote_text for e in claim.evidences] == ["5 年"]


def test_build_reports_the_score_as_a_ranking_conversion() -> None:
    """The score must not read as a model confidence (§PORT-002-D)."""
    profile = uuid4()
    candidate = _candidate(profile, 3, HardRuleOutcome.PASS)
    view = _build(profile, candidate, _resume_chunks(profile))

    assert view.report.overall_score == 90.0  # 100 - (3-1)*5
    assert "不代表模型置信度" in view.report.summary
    assert "第 3 位" in view.report.summary


def test_build_ignores_an_unrecognised_rule_id() -> None:
    """An unknown rule cannot be located, so it is downgraded rather than guessed."""
    profile = uuid4()
    rules = [_rule("has_driving_licence", HardRuleOutcome.PASS, observed="C1", required="C1")]
    candidate = _candidate_with_rules(profile, 1, HardRuleOutcome.PASS, rules)
    view = _build(profile, candidate, _resume_chunks(profile))

    claim = _claims(view)["hard_rule:has_driving_licence"]
    assert claim.claim.support_level == SupportLevel.PARTIAL
    assert claim.evidences == []


def test_build_fail_candidate_downgraded_to_insufficient() -> None:
    profile = uuid4()
    chunk = _chunk(profile)
    candidate = _candidate(profile, 2, HardRuleOutcome.FAIL)
    view = _build(profile, candidate, [chunk])
    overall = view.claims[0]
    assert overall.claim.impact_level == ImpactLevel.HIGH
    assert overall.claim.support_level == SupportLevel.INSUFFICIENT
    assert overall.claim.claim_text == INSUFFICIENT_TEMPLATE
    # No verbatim assertion is pinned for an INSUFFICIENT claim.
    assert overall.evidences == []


def test_build_without_chunks_emits_no_deterministic_evidence() -> None:
    profile = uuid4()
    candidate = _candidate(profile, 1, HardRuleOutcome.UNKNOWN)
    view = _build(profile, candidate, [])
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


def test_generate_counts_and_drops_cross_candidate_reference() -> None:
    """A chunk owned by another candidate is bound, then rejected and counted.

    The builder matches on text alone and cannot know who owns the chunk, so the
    §9.4 verifier is the last line of defence. Nothing illegal may be persisted,
    and every rejected reference must be counted — the gate metric is
    "illegal evidence references == 0", so a silent drop would hide the failure.
    """
    profile = uuid4()
    other = uuid4()
    # The chunks state the right values, but belong to `other`.
    chunks = {
        profile: [
            _chunk(other, text=text, index=index, section=section)
            for index, text, section in _RESUME
        ]
    }
    candidate = _candidate_with_rules(profile, 1, HardRuleOutcome.PASS, _passing_rules())

    # How many references the builder produced — the count the verifier must reject.
    built = _build(profile, candidate, chunks[profile])
    expected_references = sum(len(claim_view.evidences) for claim_view in built.claims)
    assert expected_references > 0

    repo, result = _generate([candidate], chunks)
    assert result.illegal_reference_count == expected_references
    assert result.reports_written == 1
    views = asyncio.run(repo.list_by_run(_run_id_of(repo)))
    assert len(views) == 1
    assert all(len(claim.evidences) == 0 for claim in views[0].claims)


def test_generate_drops_an_excerpt_from_a_superseded_profile_version() -> None:
    """A citation to an older resume version is not admissible evidence.

    Each ``CandidateProfile`` version is its own row with its own
    ``document_id``, so a chunk extracted from the previous upload carries a
    different ``candidate_profile_id``. ``evidence_chunks`` enforces this at the
    FK boundary (``fk_evidence_chunks_profile_document``); the report layer must
    not let it through either, or a superseded resume could back a current
    verdict.
    """
    current = uuid4()
    superseded = uuid4()
    chunks = {
        current: [
            _chunk(superseded, text=text, index=index, section=section)
            for index, text, section in _RESUME
        ]
    }
    candidate = _candidate_with_rules(current, 1, HardRuleOutcome.PASS, _passing_rules())
    repo, result = _generate([candidate], chunks)

    assert result.illegal_reference_count > 0
    views = asyncio.run(repo.list_by_run(_run_id_of(repo)))
    assert all(len(claim.evidences) == 0 for claim in views[0].claims)


def test_generate_keeps_a_legal_reference() -> None:
    """The counterpart: a correctly owned excerpt survives verification."""
    profile = uuid4()
    chunks = {profile: _resume_chunks(profile)}
    candidate = _candidate_with_rules(profile, 1, HardRuleOutcome.PASS, _passing_rules())
    repo, result = _generate([candidate], chunks)

    assert result.illegal_reference_count == 0
    views = asyncio.run(repo.list_by_run(_run_id_of(repo)))
    saved = [evidence for claim in views[0].claims for evidence in claim.evidences]
    assert len(saved) > 0
    # Every persisted quote is a verbatim slice of a chunk the candidate owns.
    by_id = {chunk.id: chunk for chunk in chunks[profile]}
    for evidence in saved:
        chunk = by_id[evidence.evidence_chunk_id]
        assert chunk.text[evidence.quote_start : evidence.quote_end] == evidence.quote_text


def test_generate_replaces_a_runs_previous_reports() -> None:
    """A re-run of the pass replaces its reports instead of accumulating them (§5.6).

    Reports are derived from one scoring pass. Keeping the previous pass's rows
    would leave two verdicts per candidate under a single run — and on PostgreSQL
    the second pass would collide with ``uq_match_reports_run_application``, so a
    MatchRun retry could not complete at all. The children must go with the parent;
    a claim or an evidence row left behind is an orphan no read path would ever
    surface again.
    """
    profile = uuid4()
    chunks = {profile: [_chunk(profile)]}
    candidates = [_candidate(profile, 1, HardRuleOutcome.PASS)]
    repo = InMemoryReportRepository()
    service = ReportService(repo)
    run = _agent_run(uuid4())
    match_run = _match_run(run.id)

    for _ in range(2):
        result = asyncio.run(
            service.generate_for_run(
                run=run,
                match_run=match_run,
                candidates=candidates,
                evidence=_ChunkProvider(chunks),
            )
        )
        assert result.reports_written == 1

    views = asyncio.run(repo.list_by_run(run.id))
    assert len(views) == 1
    assert len(repo._claims) == len(views[0].claims)  # noqa: SLF001
    assert len(repo._evidences) == sum(  # noqa: SLF001
        len(claim.evidences) for claim in views[0].claims
    )


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


def _bundle() -> HardRuleBundle:
    """The bundle ``hard_rule_evaluate`` would attach for a passing candidate."""
    return HardRuleBundle(
        rules=[
            HardRuleResult(
                rule_id=HardRuleId.YEARS_EXPERIENCE,
                result=HardRuleOutcome.PASS,
                reason_code="MEETS_MINIMUM",
                observed_value=5.0,
                required_value=3.0,
            ),
            HardRuleResult(
                rule_id=HardRuleId.REQUIRED_EDUCATION,
                result=HardRuleOutcome.PASS,
                reason_code="MEETS_MINIMUM",
                observed_value="本科",
                required_value="本科",
            ),
            HardRuleResult(
                rule_id=HardRuleId.REQUIRED_SKILLS,
                result=HardRuleOutcome.PASS,
                reason_code="MEETS_MINIMUM",
                observed_value=["python"],
                required_value=["python"],
            ),
        ],
        overall=HardRuleOutcome.PASS,
    )


def test_match_run_persists_reports_on_completion() -> None:
    a, b = uuid4(), uuid4()
    chunks = {a: _resume_chunks(a), b: [_chunk(b)]}
    fused = [
        FusedCandidate(
            candidate_profile_id=a,
            snapshot_order=1,
            rrf_score=9.0,
            hard_rule=_bundle(),
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

    # The PASS candidate's per-rule claims cite the excerpts stating their values.
    pass_view = next(v for v in views if v.report.candidate_profile_id == a)
    assert all(
        claim.claim.support_level == SupportLevel.SUPPORTED
        for claim in pass_view.claims
    )
    assert all(claim.evidences for claim in pass_view.claims)

    # The invariant across both reports: SUPPORTED always means cited, and a
    # claim with nothing to cite is never presented as SUPPORTED.
    for view in views:
        for claim_view in view.claims:
            if claim_view.evidences:
                assert claim_view.claim.support_level == SupportLevel.SUPPORTED
            else:
                assert claim_view.claim.support_level != SupportLevel.SUPPORTED
