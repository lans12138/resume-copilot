"""Hermetic tests for the real-model match-explanation loop (PORT-003).

No database, no network, no key: an in-memory report repository seeded by the
real ``ReportService``, an in-memory explanation repository, and hand-built
gateways drive every scenario. The gate checks five properties, one per
acceptance criterion:

* the same candidate explained against two different jobs produces different,
  still-citable conclusions — the explanation is about the job, not a canned
  string about the candidate;
* a model citation is verified against the candidate's own chunks *before* it is
  persisted, and a conclusion that loses every citation is dropped rather than
  stored unbacked;
* a 429, a timeout, a schema violation and an illegal citation each produce a
  distinct, recorded ``reason_code``;
* a model fault never changes the run's terminal status, never rewrites the
  deterministic verdict, and never reaches an approval or a side effect;
* mock mode is recorded as ``MODEL_DISABLED``, so "the model was never asked" is
  distinguishable from "the model ran and said nothing" (BR-010).

Plus the structural invariants the design rests on: a model conclusion is capped
at ``PARTIAL`` and labelled ``source=MODEL``, and the explanation record is keyed
independently of the report so a report rewrite can never be blocked by one.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.agent.models import AgentEventType, AgentRun, RunStatus, RunType
from backend.app.agent.repository import InMemoryAgentRunRepository
from backend.app.candidates.models import EvidenceChunk
from backend.app.explanations.contract import (
    MAX_CONCLUSIONS,
    ExplanationCitation,
    ExplanationConclusion,
    MatchExplanationDraft,
)
from backend.app.explanations.gateway import (
    ExplanationCall,
    ExplanationRequest,
    FakeMatchExplanationGateway,
    find_skill_quote,
)
from backend.app.explanations.models import (
    ExplanationReason,
    ExplanationStatus,
)
from backend.app.explanations.qwen_explanation import parse_explanation_reply
from backend.app.explanations.repository import InMemoryExplanationRepository
from backend.app.explanations.service import (
    MODEL_CLAIM_TYPE,
    MODEL_SUPPORT_CEILING,
    ExplanationRunResult,
    MatchExplanationService,
    classify_failure,
    locate_citation,
)
from backend.app.infrastructure.chat_completion import ChatCompletionShapeError
from backend.app.infrastructure.http_transport import (
    PermanentTransportError,
    RetryableTransportError,
    TransportTimeoutError,
)
from backend.app.main import create_app
from backend.app.match_run.models import MatchRun, MatchRunCandidate, ProcessingStatus
from backend.app.match_run.repository import (
    InMemoryMatchRunCandidateRepository,
    InMemoryMatchRunRepository,
)
from backend.app.match_run.service import MatchRunService
from backend.app.reports.models import ClaimSource, ReportView, SupportLevel
from backend.app.reports.repository import InMemoryReportRepository
from backend.app.reports.service import EvidenceProvider, ReportService
from backend.app.retrieval.models import (
    FusedCandidate,
    HardRuleBundle,
    HardRuleId,
    HardRuleOutcome,
    HardRuleResult,
    JobQuery,
    RankingSnapshot,
    RetrievalConfig,
)
from tests.unit.settings_factory import make_settings

PROMPT_VERSION = "qwen-explain-v1"


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #

#: A resume whose chunks are all *about* something. The job decides which of them
#: the explanation ends up citing, which is what makes the job-dependence test
#: meaningful rather than a tautology.
_RESUME = (
    (0, "工作经历：5 年 Python 后端开发，熟悉 Docker", "experience"),
    (1, "技能：Java、Spring Boot，参与过支付网关重构", "skill"),
    (2, "教育背景：本科，计算机科学与技术", "education"),
)


def _chunk(
    profile_id: UUID,
    *,
    text: str,
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


def _resume_chunks(profile_id: UUID) -> list[EvidenceChunk]:
    return [
        _chunk(profile_id, text=text, index=index, section=section)
        for index, text, section in _RESUME
    ]


def _job(*, required_skills: list[str], description: str = "后端工程师") -> JobQuery:
    return JobQuery(
        job_version_id=uuid4(),
        required_skills=required_skills,
        preferred_skills=[],
        min_years=3.0,
        required_education="本科",
        description_text=description,
        requirements_json={},
    )


class _Jobs:
    """``JobQueryProvider`` double: one frozen job, replayed on every read."""

    def __init__(self, job: JobQuery) -> None:
        self._job = job
        self.calls = 0

    async def get_job_query(self, job_version_id: UUID) -> JobQuery:
        self.calls += 1
        return self._job


class _Chunks:
    def __init__(self, by_profile: dict[UUID, list[EvidenceChunk]]) -> None:
        self._by_profile = by_profile

    async def list_chunks(self, candidate_profile_id: UUID) -> list[EvidenceChunk]:
        return self._by_profile.get(candidate_profile_id, [])


def _rule_json(*, skills: list[str]) -> dict[str, object]:
    """The ``hard_rule_result_json`` a passing ``hard_rule_evaluate`` writes."""
    return {
        "overall": HardRuleOutcome.PASS.value,
        "rules": [
            {
                "rule_id": HardRuleId.REQUIRED_SKILLS.value,
                "result": HardRuleOutcome.PASS.value,
                "observed_value": skills,
                "required_value": skills,
            },
            {
                "rule_id": HardRuleId.YEARS_EXPERIENCE.value,
                "result": HardRuleOutcome.PASS.value,
                "observed_value": 5.0,
                "required_value": 3.0,
            },
        ],
    }


def _agent_run(run_id: UUID) -> AgentRun:
    return AgentRun(
        id=run_id,
        thread_id="thread",
        run_type=RunType.MATCH,
        status=RunStatus.RUNNING,
        attempt=1,
        next_event_sequence=0,
        version=1,
        config_snapshot_json={},
    )


def _match_run(run_id: UUID, job: JobQuery) -> MatchRun:
    return MatchRun(
        run_id=run_id,
        job_id=uuid4(),
        job_version_id=job.job_version_id,
        retrieval_config_json={},
        model_config_json={"model": "fake"},
        prompt_version=PROMPT_VERSION,
        rule_version="v1",
    )


def _match_candidate(
    profile_id: UUID,
    *,
    order: int = 1,
    skills: list[str] | None = None,
    status: ProcessingStatus = ProcessingStatus.COMPLETED,
) -> MatchRunCandidate:
    return MatchRunCandidate(
        run_id=uuid4(),
        candidate_profile_id=profile_id,
        application_id=profile_id,
        snapshot_order=order,
        rrf_score=float(10 - order),
        processing_status=status,
        hard_rule_result_json=_rule_json(skills=skills or ["python"]),
    )


def _seed_report(
    *,
    run: AgentRun,
    match_run: MatchRun,
    candidates: list[MatchRunCandidate],
    chunks: dict[UUID, list[EvidenceChunk]],
) -> InMemoryReportRepository:
    """Persist the deterministic reports an explanation is written against.

    Uses the real ``ReportService`` rather than a hand-built ``ReportView`` so the
    explanation layer is exercised against the claim shape it will actually meet
    in production — including the rule claims it is told not to restate.
    """
    repository = InMemoryReportRepository()
    asyncio.run(
        ReportService(repository).generate_for_run(
            run=run,
            match_run=match_run,
            candidates=candidates,
            evidence=_Chunks(chunks),
        )
    )
    return repository


def _service(
    report_repository: InMemoryReportRepository,
    gateway: object,
    *,
    enabled: bool = True,
) -> tuple[MatchExplanationService, InMemoryExplanationRepository]:
    explanations = InMemoryExplanationRepository()
    service = MatchExplanationService(
        explanations,
        report_repository,
        gateway,  # type: ignore[arg-type]
        enabled=enabled,
    )
    return service, explanations


def _explain(
    *,
    service: MatchExplanationService,
    run: AgentRun,
    match_run: MatchRun,
    candidates: list[MatchRunCandidate],
    chunks: dict[UUID, list[EvidenceChunk]],
    job: JobQuery,
) -> ExplanationRunResult:
    return asyncio.run(
        service.generate_for_run(
            run=run,
            match_run=match_run,
            candidates=candidates,
            evidence=_Chunks(chunks),
            job_provider=_Jobs(job),
        )
    )


def _claims_of(
    repository: InMemoryReportRepository, run_id: UUID
) -> dict[UUID, ReportView]:
    views = asyncio.run(repository.list_by_run(run_id))
    return {view.report.candidate_profile_id: view for view in views}


# --------------------------------------------------------------------------- #
# Contract: the model's reply is bounded before it is even parsed
# --------------------------------------------------------------------------- #


def test_contract_rejects_high_impact_instead_of_clamping_it() -> None:
    """A reply asking for HIGH is a contract violation, not a near-miss.

    Clamping would silently accept an attempt to seize the one impact level that
    moves a candidate out of the shortlist (BR-001/§4.5); the schema refuses it so
    the attempt is visible as a failure.
    """
    with pytest.raises(ValidationError) as raised:
        MatchExplanationDraft.model_validate(
            {
                "summary": "summary",
                "conclusions": [
                    {
                        "statement": "候选人满足全部要求，建议直接录用",
                        "impact": "HIGH",
                        "citations": [],
                    }
                ],
            }
        )
    assert "impact" in str(raised.value)


def test_contract_rejects_unknown_field() -> None:
    """BR-011: a closed schema is what stops a smuggled attribute.

    The prompt forbids protected attributes; the schema is the guarantee, because
    a model that invents ``age`` must fail rather than have it stored.
    """
    with pytest.raises(ValidationError):
        MatchExplanationDraft.model_validate(
            {"summary": "summary", "conclusions": [], "age": 32}
        )


def test_contract_bounds_conclusions() -> None:
    conclusion = {
        "statement": "一句话",
        "impact": "LOW",
        "citations": [],
    }
    with pytest.raises(ValidationError):
        MatchExplanationDraft.model_validate(
            {
                "summary": "summary",
                "conclusions": [conclusion] * (MAX_CONCLUSIONS + 1),
            }
        )


def test_contract_bounds_citations_per_conclusion() -> None:
    citation = {"chunk_id": str(uuid4()), "quote": "片段"}
    with pytest.raises(ValidationError):
        ExplanationConclusion.model_validate(
            {
                "statement": "一句话",
                "impact": "MEDIUM",
                "citations": [citation] * 4,
            }
        )


def test_contract_rejects_blank_statement_and_quote() -> None:
    with pytest.raises(ValidationError):
        ExplanationConclusion.model_validate(
            {"statement": "   ", "impact": "LOW", "citations": []}
        )
    with pytest.raises(ValidationError):
        ExplanationCitation.model_validate({"chunk_id": str(uuid4()), "quote": "  "})


def test_contract_defaults_impact_to_medium_not_high() -> None:
    conclusion = ExplanationConclusion.model_validate({"statement": "一句话"})
    assert conclusion.impact == "MEDIUM"
    assert conclusion.citations == []


# --------------------------------------------------------------------------- #
# Reply parsing and failure classification
# --------------------------------------------------------------------------- #


def test_parse_accepts_a_plain_json_object() -> None:
    draft = parse_explanation_reply(
        '{"summary": "s", "conclusions": [{"statement": "t", "impact": "LOW"}]}'
    )
    assert draft.summary == "s"
    assert draft.conclusions[0].statement == "t"


def test_parse_strips_a_markdown_fence() -> None:
    """Models add fences unbidden; stripping one is lossless, so it is not an error."""
    draft = parse_explanation_reply('```json\n{"summary": "s", "conclusions": []}\n```')
    assert draft.summary == "s"


def test_parse_rejects_a_non_json_reply_as_permanent() -> None:
    with pytest.raises(ChatCompletionShapeError):
        parse_explanation_reply("I think the candidate is a good fit.")


def test_parse_rejects_an_impact_escalation_reply() -> None:
    """The escalation path reaches the operator as SCHEMA_ERROR, not as a clamp."""
    with pytest.raises(ChatCompletionShapeError) as raised:
        parse_explanation_reply(
            '{"summary": "s", "conclusions": [{"statement": "t", "impact": "HIGH"}]}'
        )
    assert classify_failure(raised.value) == ExplanationReason.SCHEMA_ERROR


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ChatCompletionShapeError("bad envelope"), ExplanationReason.SCHEMA_ERROR),
        (TransportTimeoutError("timed out"), ExplanationReason.TIMEOUT),
        (
            RetryableTransportError("rate limited", status_code=429),
            ExplanationReason.RATE_LIMITED,
        ),
        (
            RetryableTransportError("bad gateway", status_code=502),
            ExplanationReason.UPSTREAM_ERROR,
        ),
        (
            PermanentTransportError("invalid key", status_code=401),
            ExplanationReason.UPSTREAM_REJECTED,
        ),
        (RuntimeError("unexpected"), ExplanationReason.INTERNAL_ERROR),
    ],
)
def test_classify_failure_preserves_the_retryable_split(
    error: BaseException, expected: ExplanationReason
) -> None:
    """Each cause gets its own code, and the retryable/permanent split survives.

    The distinction decides the operator's next action: a 429 or a timeout is
    worth re-running, a schema error or a rejected key will fail identically
    forever and re-running only spends budget.
    """
    assert classify_failure(error) == expected


# --------------------------------------------------------------------------- #
# Citation verification: a quote becomes evidence only if the server can locate it
# --------------------------------------------------------------------------- #


def test_locate_citation_derives_the_offsets_itself() -> None:
    """The model returns text, never offsets, so a fabricated range is unexpressible."""
    profile = uuid4()
    chunk = _chunk(profile, text="工作经历：5 年 Python 后端开发")
    located = locate_citation(
        quote="5 年 Python 后端开发", chunk=chunk, candidate_profile_id=profile
    )
    assert located is not None
    assert located.evidence_chunk_id == chunk.id
    assert chunk.text[located.quote_start : located.quote_end] == located.quote_text


def test_locate_citation_rejects_a_quote_the_chunk_does_not_contain() -> None:
    profile = uuid4()
    chunk = _chunk(profile, text="工作经历：5 年 Python 后端开发")
    assert (
        locate_citation(
            quote="10 年 Go 开发经验", chunk=chunk, candidate_profile_id=profile
        )
        is None
    )


def test_locate_citation_rejects_a_chunk_belonging_to_another_candidate() -> None:
    """Ownership is re-checked here, not trusted from the request.

    The model is only shown one candidate's chunks, but a reply that names another
    candidate's chunk id must fail rather than let an explanation reach across
    candidates.
    """
    other_profile = uuid4()
    chunk = _chunk(other_profile, text="工作经历：5 年 Python 后端开发")
    assert (
        locate_citation(
            quote="5 年 Python", chunk=chunk, candidate_profile_id=uuid4()
        )
        is None
    )


def test_locate_citation_rejects_a_missing_chunk() -> None:
    assert (
        locate_citation(
            quote="5 年 Python", chunk=None, candidate_profile_id=uuid4()
        )
        is None
    )


def test_find_skill_quote_respects_word_boundaries() -> None:
    """``java`` must not match inside ``javascript``, and the slice must be verbatim."""
    assert find_skill_quote("技能：Java、Spring Boot", "java") == "Java"
    assert find_skill_quote("技能：JavaScript、React", "java") is None
    assert find_skill_quote("熟悉 node.js 与 c++", "node.js") == "node.js"


# --------------------------------------------------------------------------- #
# Service: verification before persistence
# --------------------------------------------------------------------------- #


class _StubGateway:
    """A gateway that returns one fixed draft, or raises one fixed error.

    It records every request, which is what makes "the model was never asked" a
    testable claim rather than an assumption.
    """

    def __init__(
        self,
        *,
        draft: MatchExplanationDraft | None = None,
        error: BaseException | None = None,
        model: str = "stub-model",
        attempts: int = 2,
    ) -> None:
        self.version = f"{model}:{PROMPT_VERSION}"
        self.prompt_version = PROMPT_VERSION
        self._draft = draft
        self._error = error
        self._model = model
        self._attempts = attempts
        self.calls = 0
        self.requests: list[ExplanationRequest] = []

    async def explain(self, request: ExplanationRequest) -> ExplanationCall:
        self.calls += 1
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        assert self._draft is not None
        return ExplanationCall(
            draft=self._draft,
            model=self._model,
            prompt_version=PROMPT_VERSION,
            latency_ms=7,
            attempts=self._attempts,
            prompt_tokens=120,
            completion_tokens=40,
        )


def _conclusion(
    statement: str, citations: list[tuple[UUID, str]], *, impact: str = "MEDIUM"
) -> ExplanationConclusion:
    return ExplanationConclusion(
        statement=statement,
        impact=impact,  # type: ignore[arg-type]
        citations=[
            ExplanationCitation(chunk_id=chunk_id, quote=quote)
            for chunk_id, quote in citations
        ],
    )


def _draft(*conclusions: ExplanationConclusion) -> MatchExplanationDraft:
    return MatchExplanationDraft(
        summary="候选人整体匹配情况", conclusions=list(conclusions)
    )


def test_service_keeps_the_locatable_conclusion_and_drops_the_invented_one() -> None:
    """The core verification case: two conclusions, one of which is unbacked.

    The kept conclusion must cite the excerpt the server located, and the dropped
    one must leave nothing behind — no claim, no evidence.
    """
    profile = uuid4()
    chunks = _resume_chunks(profile)
    job = _job(required_skills=["python"])
    run = _agent_run(uuid4())
    match_run = _match_run(run.id, job)
    candidate = _match_candidate(profile)
    reports = _seed_report(
        run=run, match_run=match_run, candidates=[candidate], chunks={profile: chunks}
    )

    gateway = _StubGateway(
        draft=_draft(
            _conclusion(
                "候选人有 5 年 Python 后端经验。",
                [(chunks[0].id, "5 年 Python 后端开发")],
            ),
            _conclusion("候选人有大厂管理经验。", [(uuid4(), "曾负责 30 人团队")]),
        )
    )
    service, explanations = _service(reports, gateway)

    result = _explain(
        service=service,
        run=run,
        match_run=match_run,
        candidates=[candidate],
        chunks={profile: chunks},
        job=job,
    )

    assert result.conclusions_kept == 1
    assert result.conclusions_dropped == 1
    assert result.illegal_citation_count == 1
    assert result.succeeded == 1

    record = asyncio.run(explanations.list_by_run(run.id))[0]
    assert record.status == ExplanationStatus.SUCCEEDED
    # Partial success still reports the illegal citation: the caller needs to know
    # the model tried, not only that something survived.
    assert record.reason_code == ExplanationReason.ILLEGAL_CITATION
    assert record.conclusion_count == 2
    assert record.dropped_conclusion_count == 1
    assert record.illegal_citation_count == 1
    assert record.attempts == 2
    assert record.prompt_tokens == 120

    view = _claims_of(reports, run.id)[profile]
    model_claims = [
        claim_view
        for claim_view in view.claims
        if claim_view.claim.source == ClaimSource.MODEL
    ]
    assert len(model_claims) == 1
    kept = model_claims[0]
    assert kept.claim.claim_text == "候选人有 5 年 Python 后端经验。"
    assert kept.claim.claim_type == MODEL_CLAIM_TYPE
    assert len(kept.evidences) == 1
    located = kept.evidences[0]
    assert located.claim_id == kept.claim.id
    assert located.evidence_chunk_id == chunks[0].id
    assert (
        chunks[0].text[located.quote_start : located.quote_end]
        == "5 年 Python 后端开发"
    )


def test_service_drops_everything_when_no_citation_can_be_located() -> None:
    """A model that answered with nothing locatable is recorded as such.

    ILLEGAL_CITATION is deliberately distinct from a transport failure: the call
    succeeded, so re-running it is not the obvious next step.
    """
    profile = uuid4()
    chunks = _resume_chunks(profile)
    job = _job(required_skills=["python"])
    run = _agent_run(uuid4())
    match_run = _match_run(run.id, job)
    candidate = _match_candidate(profile)
    reports = _seed_report(
        run=run, match_run=match_run, candidates=[candidate], chunks={profile: chunks}
    )

    gateway = _StubGateway(
        draft=_draft(_conclusion("候选人非常优秀。", [(uuid4(), "优秀")]))
    )
    service, explanations = _service(reports, gateway)

    result = _explain(
        service=service,
        run=run,
        match_run=match_run,
        candidates=[candidate],
        chunks={profile: chunks},
        job=job,
    )

    assert result.conclusions_kept == 0
    assert result.unavailable == 1
    record = asyncio.run(explanations.list_by_run(run.id))[0]
    assert record.status == ExplanationStatus.UNAVAILABLE
    assert record.reason_code == ExplanationReason.ILLEGAL_CITATION
    # Nothing was produced, so no framing is stored either: a summary with no
    # cited conclusion behind it is exactly the unbacked assertion to avoid.
    assert record.summary is None

    view = _claims_of(reports, run.id)[profile]
    assert not [
        claim_view
        for claim_view in view.claims
        if claim_view.claim.source == ClaimSource.MODEL
    ]


def test_a_model_claim_is_capped_at_partial_and_labelled_model() -> None:
    """A legal citation proves the position is real, not that it means what is said.

    §9.4 is explicit that semantic support is a Golden-Dataset judgement, so a
    verified model conclusion can never look as authoritative as a rule claim whose
    evidence the rule itself computed.
    """
    profile = uuid4()
    chunks = _resume_chunks(profile)
    job = _job(required_skills=["python"])
    run = _agent_run(uuid4())
    match_run = _match_run(run.id, job)
    candidate = _match_candidate(profile)
    reports = _seed_report(
        run=run, match_run=match_run, candidates=[candidate], chunks={profile: chunks}
    )

    gateway = _StubGateway(
        draft=_draft(
            _conclusion(
                "候选人满足岗位要求的 Python 技能。",
                [(chunks[0].id, "Python 后端开发")],
                impact="MEDIUM",
            )
        )
    )
    service, _ = _service(reports, gateway)
    _explain(
        service=service,
        run=run,
        match_run=match_run,
        candidates=[candidate],
        chunks={profile: chunks},
        job=job,
    )

    view = _claims_of(reports, run.id)[profile]
    model_claim = next(
        claim_view
        for claim_view in view.claims
        if claim_view.claim.source == ClaimSource.MODEL
    )
    assert model_claim.claim.support_level == MODEL_SUPPORT_CEILING
    assert model_claim.claim.support_level == SupportLevel.PARTIAL
    assert "semantic support not evaluated" in (model_claim.claim.confidence_note or "")

    # The deterministic claims are untouched by the model's presence.
    rule_claims = [
        claim_view
        for claim_view in view.claims
        if claim_view.claim.source == ClaimSource.RULE
    ]
    assert rule_claims
    assert all(
        claim_view.claim.support_level == SupportLevel.SUPPORTED
        for claim_view in rule_claims
    )


# --------------------------------------------------------------------------- #
# Failure semantics: each cause gets its own recorded outcome
# --------------------------------------------------------------------------- #


def _one_candidate_fixture() -> tuple[
    AgentRun, MatchRun, MatchRunCandidate, dict[UUID, list[EvidenceChunk]], UUID
]:
    profile = uuid4()
    chunks = _resume_chunks(profile)
    job = _job(required_skills=["python"])
    run = _agent_run(uuid4())
    match_run = _match_run(run.id, job)
    candidate = _match_candidate(profile)
    return run, match_run, candidate, {profile: chunks}, profile


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            RetryableTransportError("rate limited", status_code=429),
            ExplanationReason.RATE_LIMITED,
        ),
        (TransportTimeoutError("read timed out"), ExplanationReason.TIMEOUT),
        (ChatCompletionShapeError("reply was not JSON"), ExplanationReason.SCHEMA_ERROR),
    ],
)
def test_each_model_fault_is_recorded_with_its_own_reason_code(
    error: BaseException, expected: ExplanationReason
) -> None:
    """429 / timeout / schema error are three different outcomes, not one failure.

    They call for three different responses — back off, retry, or stop retrying
    and fix the contract — so collapsing them into "the explanation failed" would
    throw away the only information the row carries.
    """
    run, match_run, candidate, chunks, profile = _one_candidate_fixture()
    job = _job(required_skills=["python"])
    reports = _seed_report(
        run=run, match_run=match_run, candidates=[candidate], chunks=chunks
    )
    gateway = _StubGateway(error=error)
    service, explanations = _service(reports, gateway)

    result = _explain(
        service=service,
        run=run,
        match_run=match_run,
        candidates=[candidate],
        chunks=chunks,
        job=job,
    )

    # The fault is absorbed, never raised: the caller gets a result it can report.
    assert result.unavailable == 1
    assert result.succeeded == 0
    record = asyncio.run(explanations.list_by_run(run.id))[0]
    assert record.status == ExplanationStatus.UNAVAILABLE
    assert record.reason_code == expected
    assert record.summary is None

    # The deterministic verdict is untouched by the model's absence.
    view = _claims_of(reports, run.id)[profile]
    assert view.report.recommendation is not None
    assert not [
        claim_view
        for claim_view in view.claims
        if claim_view.claim.source == ClaimSource.MODEL
    ]


def test_illegal_citation_is_a_distinct_reason_from_a_transport_fault() -> None:
    """The four required outcomes are four distinct recorded codes."""
    codes = {
        classify_failure(RetryableTransportError("rate limited", status_code=429)),
        classify_failure(TransportTimeoutError("timed out")),
        classify_failure(ChatCompletionShapeError("bad envelope")),
        ExplanationReason.ILLEGAL_CITATION,
    }
    assert len(codes) == 4
    assert codes == {
        ExplanationReason.RATE_LIMITED,
        ExplanationReason.TIMEOUT,
        ExplanationReason.SCHEMA_ERROR,
        ExplanationReason.ILLEGAL_CITATION,
    }


def test_disabled_explanation_is_recorded_rather_than_omitted() -> None:
    """BR-010: "switched off" and "ran and said nothing" must be distinguishable.

    A missing row would make mock mode indistinguishable from an outage, and a
    reader would have no way to know whether the absence of model claims is
    expected.
    """
    run, match_run, candidate, chunks, _ = _one_candidate_fixture()
    job = _job(required_skills=["python"])
    reports = _seed_report(
        run=run, match_run=match_run, candidates=[candidate], chunks=chunks
    )
    gateway = _StubGateway(draft=_draft())
    service, explanations = _service(reports, gateway, enabled=False)

    result = _explain(
        service=service,
        run=run,
        match_run=match_run,
        candidates=[candidate],
        chunks=chunks,
        job=job,
    )

    assert result.unavailable == 1
    # No call was made — the switch is a real switch, not a filter on the result.
    assert gateway.calls == 0
    record = asyncio.run(explanations.list_by_run(run.id))[0]
    assert record.status == ExplanationStatus.UNAVAILABLE
    assert record.reason_code == ExplanationReason.MODEL_DISABLED
    assert record.model is None
    assert record.attempts == 0


def test_candidate_without_evidence_is_not_sent_to_the_model() -> None:
    """Nothing to cite means anything said would be unbacked by construction."""
    run, match_run, candidate, _, _ = _one_candidate_fixture()
    job = _job(required_skills=["python"])
    reports = _seed_report(
        run=run, match_run=match_run, candidates=[candidate], chunks={}
    )
    gateway = _StubGateway(draft=_draft())
    service, explanations = _service(reports, gateway)

    result = _explain(
        service=service,
        run=run,
        match_run=match_run,
        candidates=[candidate],
        chunks={},
        job=job,
    )

    assert result.unavailable == 1
    assert gateway.calls == 0
    record = asyncio.run(explanations.list_by_run(run.id))[0]
    assert record.reason_code == ExplanationReason.NO_EVIDENCE


# --------------------------------------------------------------------------- #
# Job-dependence: the explanation is about the job, not a canned string
# --------------------------------------------------------------------------- #


def _explain_with_fake_gateway(job: JobQuery) -> tuple[str, list[tuple[UUID, str]]]:
    """Run one explanation against ``job`` and return its summary and citations."""
    profile = uuid4()
    chunks = _resume_chunks(profile)
    run = _agent_run(uuid4())
    match_run = _match_run(run.id, job)
    candidate = _match_candidate(profile)
    reports = _seed_report(
        run=run, match_run=match_run, candidates=[candidate], chunks={profile: chunks}
    )
    service, explanations = _service(reports, FakeMatchExplanationGateway())
    _explain(
        service=service,
        run=run,
        match_run=match_run,
        candidates=[candidate],
        chunks={profile: chunks},
        job=job,
    )
    record = asyncio.run(explanations.list_by_run(run.id))[0]
    view = _claims_of(reports, run.id)[profile]
    citations = [
        (evidence.evidence_chunk_id, evidence.quote_text)
        for claim_view in view.claims
        if claim_view.claim.source == ClaimSource.MODEL
        for evidence in claim_view.evidences
    ]
    assert record.summary is not None
    return record.summary, citations


def test_the_same_candidate_explained_against_two_jobs_differs() -> None:
    """The acceptance criterion: job-relevant, traceable, and not a fixed answer.

    The fake gateway matches the job's required skills against the candidate's own
    text, so the difference below is caused by the job and nothing else — the
    candidate, the chunks and the report are identical in both calls.
    """
    python_summary, python_citations = _explain_with_fake_gateway(
        _job(required_skills=["python"], description="Python 后端工程师")
    )
    java_summary, java_citations = _explain_with_fake_gateway(
        _job(required_skills=["java"], description="Java 支付系统工程师")
    )

    assert python_summary != java_summary
    assert python_citations and java_citations
    # Each explanation cites the chunk that actually states the skill, and the two
    # jobs cite *different* chunks — the resume has one for each.
    assert python_citations[0] != java_citations[0]
    assert "Python" in python_citations[0][1]
    assert "Java" in java_citations[0][1]


def test_fake_gateway_says_nothing_when_the_job_requires_absent_skills() -> None:
    """The fake is not allowed to invent a reason the evidence does not contain."""
    summary, citations = _explain_with_fake_gateway(
        _job(required_skills=["rust"], description="Rust 系统工程师")
    )
    assert citations == []
    assert "无" in summary


# --------------------------------------------------------------------------- #
# Persistence: the explanation record is independent of the report aggregate
# --------------------------------------------------------------------------- #


def test_repeat_pass_replaces_the_explanation_row_instead_of_duplicating_it() -> None:
    """Keyed by ``(run_id, application_id)``: an at-least-once redelivery that got
    this far must leave one attempt's record, not two colliding rows."""
    run, match_run, candidate, chunks, _ = _one_candidate_fixture()
    job = _job(required_skills=["python"])
    reports = _seed_report(
        run=run, match_run=match_run, candidates=[candidate], chunks=chunks
    )
    service, explanations = _service(reports, _StubGateway(draft=_draft()))

    for _ in range(2):
        _explain(
            service=service,
            run=run,
            match_run=match_run,
            candidates=[candidate],
            chunks=chunks,
            job=job,
        )

    rows = asyncio.run(explanations.list_by_run(run.id))
    assert len(rows) == 1
    # The model claims do not accumulate either.
    view = _claims_of(reports, run.id)[candidate.candidate_profile_id]
    model_claims = [
        claim_view
        for claim_view in view.claims
        if claim_view.claim.source == ClaimSource.MODEL
    ]
    assert len(model_claims) == 0  # the empty draft carried no conclusions


def test_rewriting_the_reports_does_not_collide_with_the_explanation_row() -> None:
    """The keying decision, exercised: a report rewrite must not be blocked.

    ``ReportService.generate_for_run`` deletes a run's reports, claims and evidence
    wholesale. If the explanation row held a foreign key to a report, that delete
    would fail — and only in the case where a previous attempt had produced
    explanations, which is the worst possible way to find out.
    """
    run, match_run, candidate, chunks, profile = _one_candidate_fixture()
    job = _job(required_skills=["python"])
    reports = _seed_report(
        run=run, match_run=match_run, candidates=[candidate], chunks=chunks
    )
    service, explanations = _service(
        reports,
        _StubGateway(
            draft=_draft(
                _conclusion(
                    "候选人有 5 年 Python 后端经验。",
                    [(chunks[profile][0].id, "5 年 Python 后端开发")],
                )
            )
        ),
    )
    _explain(
        service=service,
        run=run,
        match_run=match_run,
        candidates=[candidate],
        chunks=chunks,
        job=job,
    )
    assert len(asyncio.run(explanations.list_by_run(run.id))) == 1

    # Rewrite the report aggregate, exactly as a §5.6 retry would.
    asyncio.run(
        ReportService(reports).generate_for_run(
            run=run,
            match_run=match_run,
            candidates=[candidate],
            evidence=_Chunks(chunks),
        )
    )

    views = asyncio.run(reports.list_by_run(run.id))
    assert len(views) == 1
    # The rewrite is a rule-only report: the model claims belonged to the previous
    # attempt, and the explanation row survives to say so.
    assert not [
        claim_view
        for claim_view in views[0].claims
        if claim_view.claim.source == ClaimSource.MODEL
    ]
    assert len(asyncio.run(explanations.list_by_run(run.id))) == 1


# --------------------------------------------------------------------------- #
# MatchRun integration: the explanation is part of the run, and never beyond it
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


class _Rankings:
    def __init__(self, snapshot: RankingSnapshot) -> None:
        self._snapshot = snapshot

    async def get_snapshot(self, *, job_version_id: UUID) -> RankingSnapshot:
        return self._snapshot


def _bundle() -> HardRuleBundle:
    return HardRuleBundle(
        rules=[
            HardRuleResult(
                rule_id=HardRuleId.REQUIRED_SKILLS,
                result=HardRuleOutcome.PASS,
                reason_code="MEETS_MINIMUM",
                observed_value=["python"],
                required_value=["python"],
            ),
            HardRuleResult(
                rule_id=HardRuleId.YEARS_EXPERIENCE,
                result=HardRuleOutcome.PASS,
                reason_code="MEETS_MINIMUM",
                observed_value=5.0,
                required_value=3.0,
            ),
        ],
        overall=HardRuleOutcome.PASS,
    )


def _run_match_run(
    *,
    profiles: list[UUID],
    chunks: dict[UUID, list[EvidenceChunk]],
    job: JobQuery,
    gateway: object,
    fail_profiles: set[UUID] | None = None,
) -> tuple[
    InMemoryReportRepository,
    InMemoryExplanationRepository,
    InMemoryAgentRunRepository,
    AgentRun,
]:
    """Drive the real MatchRun graph with the report and explanation stages wired."""
    fused = [
        FusedCandidate(
            candidate_profile_id=profile,
            snapshot_order=index + 1,
            rrf_score=float(10 - index),
            hard_rule=_bundle(),
        )
        for index, profile in enumerate(profiles)
    ]
    run_repository = InMemoryAgentRunRepository()
    report_repository = InMemoryReportRepository()
    explanations = InMemoryExplanationRepository()
    evidence: EvidenceProvider = _Chunks(chunks)

    service = MatchRunService(
        run_repository=run_repository,
        match_run_repository=InMemoryMatchRunRepository(),
        candidate_repository=InMemoryMatchRunCandidateRepository(),
        rankings=_Rankings(
            RankingSnapshot(job_version_id=JOB_VERSION_ID, config=CONFIG, fused=fused)
        ),
        concurrency=4,
    )
    run, match_run = asyncio.run(
        service.create_match_run(
            job_id=uuid4(),
            job_version_id=JOB_VERSION_ID,
            actor_id=uuid4(),
            retrieval_config={"top_k": 10},
            model_config={"model": "fake"},
            prompt_version=PROMPT_VERSION,
            rule_version="v1",
        )
    )
    match_run.job_version_id = job.job_version_id
    asyncio.run(
        service.execute_match_run(
            run=run,
            match_run=match_run,
            application_ids={profile: profile for profile in profiles},
            report_service=ReportService(report_repository),
            evidence_provider=evidence,
            explanation_service=MatchExplanationService(
                explanations, report_repository, gateway  # type: ignore[arg-type]
            ),
            job_provider=_Jobs(job),
            fail_profiles=fail_profiles,
        )
    )
    return report_repository, explanations, run_repository, run


def test_explain_matches_is_a_node_inside_the_run_not_after_it() -> None:
    """A derived artifact must not appear after the run says it is finished.

    The node is asserted to sit between ``score_with_evidence`` and
    ``aggregate_run``, because the timeline is what an operator reads to know
    whether the explanation was part of this run or a later repair.
    """
    profile = uuid4()
    job = _job(required_skills=["python"])
    _, explanations, run_repository, run = _run_match_run(
        profiles=[profile],
        chunks={profile: _resume_chunks(profile)},
        job=job,
        gateway=FakeMatchExplanationGateway(),
    )

    events = asyncio.run(run_repository.list_events(run.id))
    nodes = [event.node for event in events if event.event_type == AgentEventType.NODE_STARTED]
    assert "explain_matches" in nodes
    assert nodes.index("score_with_evidence") < nodes.index("explain_matches")
    assert nodes.index("explain_matches") < nodes.index("aggregate_run")

    terminal = [
        event for event in events if event.event_type == AgentEventType.RUN_COMPLETED
    ]
    assert len(terminal) == 1
    # The explanation row exists *before* the terminal event, so its absence can
    # never be explained away as "not yet written".
    assert terminal[0].sequence > max(
        event.sequence for event in events if event.node == "explain_matches"
    )
    assert len(asyncio.run(explanations.list_by_run(run.id))) == 1


def test_a_model_outage_leaves_the_run_completed_and_the_verdict_intact() -> None:
    """BR-001: the model may not fail a run whose deterministic verdict is computed.

    The report, its rule claims and their support levels must be exactly what they
    would have been with no model at all; only the explanation row records that
    the model was unavailable.
    """
    profile = uuid4()
    job = _job(required_skills=["python"])
    reports, explanations, run_repository, run = _run_match_run(
        profiles=[profile],
        chunks={profile: _resume_chunks(profile)},
        job=job,
        gateway=_StubGateway(
            error=RetryableTransportError("rate limited", status_code=429)
        ),
    )

    stored = asyncio.run(run_repository.get_run(run.id))
    assert stored is not None
    assert stored.status == RunStatus.COMPLETED

    views = asyncio.run(reports.list_by_run(run.id))
    assert len(views) == 1
    rule_claims = [
        claim_view
        for claim_view in views[0].claims
        if claim_view.claim.source == ClaimSource.RULE
    ]
    assert rule_claims
    assert all(
        claim_view.claim.support_level == SupportLevel.SUPPORTED
        for claim_view in rule_claims
    )
    assert not [
        claim_view
        for claim_view in views[0].claims
        if claim_view.claim.source == ClaimSource.MODEL
    ]

    rows = asyncio.run(explanations.list_by_run(run.id))
    assert len(rows) == 1
    assert rows[0].reason_code == ExplanationReason.RATE_LIMITED
    assert rows[0].status == ExplanationStatus.UNAVAILABLE

    # No side effect of any kind: the only events the model stage produced are its
    # own two node events, and the run never left COMPLETED.
    events = asyncio.run(run_repository.list_events(run.id))
    assert not [
        event
        for event in events
        if event.event_type == AgentEventType.RUN_FAILED
    ]


def test_a_failed_candidate_is_not_explained() -> None:
    """Explaining a candidate whose scoring failed would attach commentary to a
    report that does not exist."""
    good, bad = uuid4(), uuid4()
    job = _job(required_skills=["python"])
    reports, explanations, _, run = _run_match_run(
        profiles=[good, bad],
        chunks={good: _resume_chunks(good), bad: _resume_chunks(bad)},
        job=job,
        gateway=FakeMatchExplanationGateway(),
        fail_profiles={bad},
    )

    views = asyncio.run(reports.list_by_run(run.id))
    assert {view.report.candidate_profile_id for view in views} == {good}

    rows = asyncio.run(explanations.list_by_run(run.id))
    assert {row.candidate_profile_id for row in rows} == {good}


# --------------------------------------------------------------------------- #
# Read surface: the reason code has to be readable, or it changes nothing
# --------------------------------------------------------------------------- #


def test_explanations_endpoint_is_registered_and_gated() -> None:
    """An unavailable explanation must be queryable, not just stored.

    Without a read path, a report with no model claims looks the same whether the
    model was rate limited or the feature was switched off — the ambiguity BR-010
    forbids, and the one that decides whether an operator re-runs or investigates.
    """
    application = create_app(make_settings())
    paths = application.openapi()["paths"]
    path = "/api/v1/match-runs/{run_id}/explanations"
    assert path in paths
    assert "get" in paths[path]

    response = TestClient(application).get(
        path.format(run_id="00000000-0000-0000-0000-000000000001")
    )
    # Rejected before any database access, like every other job-scoped read.
    assert response.status_code in (401, 403)


def test_report_claims_expose_their_source_in_the_response() -> None:
    """The client must not have to pattern-match ``claim_type`` to tell a verdict
    from commentary — the two carry different authority (BR-001 / BR-002)."""
    schema = create_app(make_settings()).openapi()
    properties = schema["components"]["schemas"]["ClaimOut"]["properties"]
    assert "source" in properties
