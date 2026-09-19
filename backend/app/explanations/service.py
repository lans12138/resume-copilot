"""Match-explanation orchestration: verify, degrade, persist (PORT-003).

The service sits between an untrusted model reply and the report, and its job is
to make the second one safe to read. Four properties it enforces:

**Nothing unbacked reaches the report.** Every citation a model offers is
re-checked against the candidate's *own* chunks: the chunk must exist, the quote
must occur verbatim in its text, and the resulting slice must pass the same §9.4
check the deterministic claims pass. A conclusion that loses every citation is
dropped and counted — never stored as an assertion with nothing behind it.

**A model conclusion is never SUPPORTED.** §9.4 is explicit that a legal
reference proves the *position* is real, not that the text semantically supports
the statement; semantic support is a Golden-Dataset judgement. So a verified
model conclusion is capped at ``PARTIAL``. The consequence is worth stating
plainly: a model claim can never look as authoritative as a rule claim whose
evidence the rule itself computed, which is exactly the ordering BR-002 asks for.

**A model failure never fails the run.** The deterministic verdict and its report
are already persisted by the time this runs. An upstream 429, a timeout, a
malformed reply or an invented chunk id is recorded as a reason code on the
explanation row and the run stays ``COMPLETED``. There is no path from here to an
approval, a side effect or a status change (BR-001, BR-007): this service writes
report claims and one call record, and nothing else.

**Model conclusions are numbered after the report's own, and replace them on a
retry.** The two writers share one ``display_order`` column on ``report_claims``
(``uq_report_claims_order`` is ``(report_id, display_order)``), so restarting the
sequence at 1 would collide with the first rule claim of every report that has
one. The pass also drops the model claims it wrote last time before writing new
ones: the deterministic pass owns the rule claims and rewrites them wholesale,
but nothing else would ever remove this service's own rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

from backend.app.agent.models import AgentRun
from backend.app.candidates.models import EvidenceChunk
from backend.app.explanations.contract import (
    ExplanationConclusion,
    MatchExplanationDraft,
)
from backend.app.explanations.gateway import (
    EvidenceItem,
    ExplanationCall,
    ExplanationRequest,
    MatchExplanationGateway,
)
from backend.app.explanations.models import (
    ExplanationReason,
    ExplanationStatus,
    MatchExplanation,
)
from backend.app.explanations.repository import ExplanationRepository
from backend.app.infrastructure.chat_completion import ChatCompletionShapeError
from backend.app.infrastructure.http_transport import (
    PermanentTransportError,
    RetryableTransportError,
    TransportTimeoutError,
)
from backend.app.match_run.models import MatchRun, MatchRunCandidate, ProcessingStatus
from backend.app.reports.models import (
    ClaimEvidence,
    ClaimSource,
    ClaimView,
    ImpactLevel,
    ReportClaim,
    ReportView,
    SupportLevel,
)
from backend.app.reports.repository import ReportRepository
from backend.app.reports.service import EvidenceProvider
from backend.app.reports.validation import (
    apply_high_impact_guard,
    verify_evidence_reference,
)
from backend.app.retrieval.models import JobQuery

logger = logging.getLogger(__name__)

#: ``claim_type`` for a model-authored conclusion. Deliberately distinct from the
#: ``hard_rule:*`` namespace so the two can never be confused even if the
#: ``source`` column is not selected.
MODEL_CLAIM_TYPE = "model_conclusion"

#: The support ceiling for model output. See the module docstring.
MODEL_SUPPORT_CEILING = SupportLevel.PARTIAL


class JobQueryProvider(Protocol):
    """Supplies the frozen job requirements a MatchRun scored against (§10.2)."""

    async def get_job_query(self, job_version_id: UUID) -> JobQuery: ...


@dataclass(frozen=True)
class ExplanationRunResult:
    """Outcome of generating explanations for one MatchRun."""

    candidates_total: int
    succeeded: int
    unavailable: int
    conclusions_kept: int
    conclusions_dropped: int
    illegal_citation_count: int


@dataclass(frozen=True)
class _VerifiedConclusion:
    """One model conclusion that survived citation verification."""

    claim: ReportClaim
    evidences: list[ClaimEvidence]


def classify_failure(error: BaseException) -> ExplanationReason:
    """Map a gateway failure to the reason code an operator will read.

    The transport's retryable/permanent split is preserved rather than collapsed
    into "failed", because the two call for opposite responses: a 429 or a timeout
    is worth re-running, while a schema error or a rejected request will fail
    identically forever and re-running only spends budget.
    """
    if isinstance(error, ChatCompletionShapeError):
        # Includes a reply that asked for HIGH impact: the schema rejects it, and
        # that is a contract violation, not a transient fault.
        return ExplanationReason.SCHEMA_ERROR
    if isinstance(error, TransportTimeoutError):
        return ExplanationReason.TIMEOUT
    if isinstance(error, RetryableTransportError):
        if error.status_code == 429:
            return ExplanationReason.RATE_LIMITED
        return ExplanationReason.UPSTREAM_ERROR
    if isinstance(error, PermanentTransportError):
        return ExplanationReason.UPSTREAM_REJECTED
    return ExplanationReason.INTERNAL_ERROR


def locate_citation(
    *, quote: str, chunk: EvidenceChunk | None, candidate_profile_id: UUID
) -> ClaimEvidence | None:
    """Turn a model-supplied quote into a verified evidence row, or ``None``.

    The model returns *text*, never offsets, so the slice is derived here — a
    fabricated range is not expressible. A missing chunk (an invented id, or one
    belonging to another candidate) and a quote that does not occur verbatim in
    the text both yield ``None``, and the derived slice is then handed to the same
    §9.4 verifier the deterministic claims use, so the two kinds of claim cannot
    drift apart in what counts as a legal reference.
    """
    if chunk is None:
        return None
    start = chunk.text.find(quote)
    if start == -1:
        return None
    candidate_row = ClaimEvidence(
        id=uuid4(),
        claim_id=uuid4(),
        evidence_chunk_id=chunk.id,
        quote_text=quote,
        quote_start=start,
        quote_end=start + len(quote),
    )
    verdict = verify_evidence_reference(
        evidence=candidate_row, chunk=chunk, candidate_profile_id=candidate_profile_id
    )
    return candidate_row if verdict.legal else None


def next_display_order(claims: list[ClaimView]) -> int:
    """Where this writer's numbering starts on a report that already has claims.

    ``uq_report_claims_order`` is ``(report_id, display_order)``, and two writers
    share that one column: ``ReportService`` numbers the deterministic claims from
    0 (the aggregate verdict) through n, and this service appends model
    conclusions to the same report. Restarting at 1 collides with the first rule
    claim of every report that has one — an ``IntegrityError`` raised inside the
    pass, which the worker retries and then abandons, leaving the run without a
    terminal status. Continuing the sequence also reads correctly: the verdict and
    the rules it is built from come first, the commentary on them after.
    """
    return max((view.claim.display_order for view in claims), default=-1) + 1


def _verdict_text(rule_claims: list[ClaimView]) -> str:
    """Render the deterministic verdicts as fixed context for the prompt.

    Only ``RULE`` claims are included. Feeding the model its own previous
    conclusions would let one run's output become the next run's premise.
    """
    lines = [
        claim_view.claim.claim_text
        for claim_view in rule_claims
        if claim_view.claim.source == ClaimSource.RULE
    ]
    return "\n".join(lines) if lines else "（本次未产生确定性硬性条件结论）"


def _job_text(job: JobQuery) -> str:
    parts = [job.description_text.strip()] if job.description_text.strip() else []
    if job.required_skills:
        parts.append("必备技能：" + "、".join(job.required_skills))
    if job.preferred_skills:
        parts.append("加分技能：" + "、".join(job.preferred_skills))
    if job.required_education:
        parts.append(f"学历要求：{job.required_education}")
    if job.min_years is not None:
        parts.append(f"年限要求：{job.min_years}")
    return "\n".join(parts) if parts else "（岗位未填写结构化要求）"


class MatchExplanationService:
    """Generate, verify and persist model explanations for a MatchRun."""

    def __init__(
        self,
        repository: ExplanationRepository,
        report_repository: ReportRepository,
        gateway: MatchExplanationGateway,
        *,
        enabled: bool = True,
    ) -> None:
        self._explanations = repository
        self._reports = report_repository
        self._gateway = gateway
        # ``enabled=False`` is the mock-mode / feature-off path. It still writes a
        # row per report, marked MODEL_DISABLED, because BR-010 requires mock
        # results to be *identifiable* in the UI — "there is no model explanation"
        # and "the model was never asked" are different facts, and only one of
        # them is a reason to distrust the rest of the report.
        self._enabled = enabled

    async def generate_for_run(
        self,
        *,
        run: AgentRun,
        match_run: MatchRun,
        candidates: list[MatchRunCandidate],
        evidence: EvidenceProvider,
        job_provider: JobQueryProvider,
    ) -> ExplanationRunResult:
        """Explain every COMPLETED candidate's report; never raise on model faults."""
        await self._explanations.delete_by_run(run.id)
        # The model claims an earlier attempt wrote go too. ``ReportService``
        # replaces its own rows by deleting the whole report, but this service
        # annotates a report it does not own, so nothing else would ever remove
        # its previous conclusions — a retry would leave two attempts' model
        # claims on one report and collide on ``uq_report_claims_order`` while
        # adding them. Rule claims are untouched: they belong to ``ReportService``
        # and are still what is being commented on.
        await self._reports.delete_claims_by_source(run.id, ClaimSource.MODEL)
        report_by_profile = {
            view.report.candidate_profile_id: view
            for view in await self._reports.list_by_run(run.id)
        }
        job = await job_provider.get_job_query(match_run.job_version_id)

        total = succeeded = unavailable = kept = dropped = illegal = 0
        for candidate in candidates:
            if candidate.processing_status != ProcessingStatus.COMPLETED:
                continue
            report_view = report_by_profile.get(candidate.candidate_profile_id)
            if report_view is None:
                # No report means nothing to explain against; the report pass is
                # the source of truth for which candidates have one.
                continue
            total += 1
            chunks = await evidence.list_chunks(candidate.candidate_profile_id)
            record, claims, counters = await self._explain_one(
                run=run,
                match_run=match_run,
                candidate=candidate,
                report_view=report_view,
                job=job,
                chunks=chunks,
            )
            await self._explanations.save(record)
            for verified in claims:
                await self._reports.save_claim(verified.claim)
                for evidence_row in verified.evidences:
                    await self._reports.save_evidence(evidence_row)
            if record.status == ExplanationStatus.SUCCEEDED:
                succeeded += 1
            else:
                unavailable += 1
            kept += counters.kept
            dropped += counters.dropped
            illegal += counters.illegal

        return ExplanationRunResult(
            candidates_total=total,
            succeeded=succeeded,
            unavailable=unavailable,
            conclusions_kept=kept,
            conclusions_dropped=dropped,
            illegal_citation_count=illegal,
        )

    async def _explain_one(
        self,
        *,
        run: AgentRun,
        match_run: MatchRun,
        candidate: MatchRunCandidate,
        report_view: ReportView,
        job: JobQuery,
        chunks: list[EvidenceChunk],
    ) -> tuple[MatchExplanation, list[_VerifiedConclusion], _Counters]:
        if not self._enabled:
            return (
                self._record(
                    run_id=run.id,
                    application_id=candidate.application_id,
                    candidate_profile_id=candidate.candidate_profile_id,
                    prompt_version=self._gateway.prompt_version,
                    rule_version=match_run.rule_version,
                    status=ExplanationStatus.UNAVAILABLE,
                    reason=ExplanationReason.MODEL_DISABLED,
                ),
                [],
                _Counters(),
            )
        if not chunks:
            # Nothing to cite, so anything the model said would be unbacked by
            # construction. Skipping the call also avoids paying for a reply that
            # could only be thrown away.
            return (
                self._record(
                    run_id=run.id,
                    application_id=candidate.application_id,
                    candidate_profile_id=candidate.candidate_profile_id,
                    prompt_version=self._gateway.prompt_version,
                    rule_version=match_run.rule_version,
                    status=ExplanationStatus.UNAVAILABLE,
                    reason=ExplanationReason.NO_EVIDENCE,
                ),
                [],
                _Counters(),
            )

        request = ExplanationRequest(
            job_text=_job_text(job),
            verdict_text=_verdict_text(report_view.claims),
            required_skills=tuple(job.required_skills),
            evidence=tuple(EvidenceItem(chunk_id=chunk.id, text=chunk.text) for chunk in chunks),
        )

        try:
            call = await self._gateway.explain(request)
        except Exception as error:  # noqa: BLE001 - the model boundary is untrusted
            reason = classify_failure(error)
            logger.warning(
                "match_explanation_failed",
                extra={
                    "run_id": str(run.id),
                    "reason_code": reason.value,
                    "error_type": type(error).__name__,
                    "retryable": reason
                    in {
                        ExplanationReason.RATE_LIMITED,
                        ExplanationReason.TIMEOUT,
                        ExplanationReason.UPSTREAM_ERROR,
                    },
                },
            )
            return (
                self._record(
                    run_id=run.id,
                    application_id=candidate.application_id,
                    candidate_profile_id=candidate.candidate_profile_id,
                    prompt_version=self._gateway.prompt_version,
                    rule_version=match_run.rule_version,
                    status=ExplanationStatus.UNAVAILABLE,
                    reason=reason,
                ),
                [],
                _Counters(),
            )

        claims, counters = self._verify(
            draft=call.draft,
            report_id=report_view.report.id,
            candidate_profile_id=candidate.candidate_profile_id,
            chunks=chunks,
            # The report's claims were read back *after* this pass removed its own
            # previous model claims, so this is genuinely the first free slot.
            first_display_order=next_display_order(report_view.claims),
        )
        status = ExplanationStatus.SUCCEEDED
        reason = ExplanationReason.OK
        if counters.dropped and not counters.kept:
            # The model answered, but nothing it said could be located. Recorded
            # as its own reason so it is not confused with a transport failure.
            status = ExplanationStatus.UNAVAILABLE
            reason = ExplanationReason.ILLEGAL_CITATION
        elif counters.illegal:
            reason = ExplanationReason.ILLEGAL_CITATION

        record = self._record(
            run_id=run.id,
            application_id=candidate.application_id,
            candidate_profile_id=candidate.candidate_profile_id,
            prompt_version=self._gateway.prompt_version,
            rule_version=match_run.rule_version,
            status=status,
            reason=reason,
            call=call,
            summary=call.draft.summary if status == ExplanationStatus.SUCCEEDED else None,
            conclusion_count=counters.offered,
            dropped_conclusion_count=counters.dropped,
            illegal_citation_count=counters.illegal,
        )
        return record, claims, counters

    def _verify(
        self,
        *,
        draft: MatchExplanationDraft,
        report_id: UUID,
        candidate_profile_id: UUID,
        chunks: list[EvidenceChunk],
        first_display_order: int,
    ) -> tuple[list[_VerifiedConclusion], _Counters]:
        """Keep only conclusions the server can back with the candidate's own text."""
        by_id = {chunk.id: chunk for chunk in chunks}
        kept: list[_VerifiedConclusion] = []
        counters = _Counters(offered=len(draft.conclusions))

        for index, conclusion in enumerate(draft.conclusions):
            evidences, illegal = self._verify_citations(
                conclusion=conclusion, by_id=by_id, candidate_profile_id=candidate_profile_id
            )
            counters.illegal += illegal
            if not evidences:
                counters.dropped += 1
                continue
            kept.append(
                self._build_claim(
                    report_id=report_id,
                    conclusion=conclusion,
                    evidences=evidences,
                    # Continues the report's own sequence rather than restarting
                    # it — see ``next_display_order``.
                    display_order=first_display_order + len(kept),
                    index=index,
                )
            )
        counters.kept = len(kept)
        return kept, counters

    def _verify_citations(
        self,
        *,
        conclusion: ExplanationConclusion,
        by_id: dict[UUID, EvidenceChunk],
        candidate_profile_id: UUID,
    ) -> tuple[list[ClaimEvidence], int]:
        verified: list[ClaimEvidence] = []
        illegal = 0
        for citation in conclusion.citations:
            chunk = by_id.get(citation.chunk_id)
            if chunk is None:
                # An invented or foreign chunk id: the model is not allowed to
                # reach outside the candidate it was given.
                illegal += 1
                continue
            located = locate_citation(
                quote=citation.quote,
                chunk=chunk,
                candidate_profile_id=candidate_profile_id,
            )
            if located is None:
                illegal += 1
                continue
            verified.append(located)
        return verified, illegal

    def _build_claim(
        self,
        *,
        report_id: UUID,
        conclusion: ExplanationConclusion,
        evidences: list[ClaimEvidence],
        display_order: int,
        index: int,
    ) -> _VerifiedConclusion:
        claim_id = uuid4()
        claim = ReportClaim(
            id=claim_id,
            report_id=report_id,
            claim_type=MODEL_CLAIM_TYPE,
            claim_text=conclusion.statement,
            source=ClaimSource.MODEL,
            impact_level=ImpactLevel(conclusion.impact),
            # Capped at PARTIAL: the citation proves the quote is real, not that
            # it means what the model says it means (module docstring).
            support_level=MODEL_SUPPORT_CEILING,
            confidence_note=(
                f"source=model; citations={len(evidences)}; "
                "semantic support not evaluated"
            ),
            display_order=display_order,
        )
        # Defensive only: the schema forbids HIGH, so this is unreachable through
        # the gateway. It is here because a hand-built double or a replayed
        # fixture is another way in, and the §4.5 rule has to hold for every path.
        claim_text, support = apply_high_impact_guard(
            impact_level=claim.impact_level,
            support_level=claim.support_level,
            claim_text=claim.claim_text,
        )
        claim.claim_text = claim_text
        claim.support_level = support
        for evidence_row in evidences:
            evidence_row.claim_id = claim_id
        return _VerifiedConclusion(claim=claim, evidences=evidences)

    def _record(
        self,
        *,
        run_id: UUID,
        application_id: UUID,
        candidate_profile_id: UUID,
        prompt_version: str,
        rule_version: str,
        status: ExplanationStatus,
        reason: ExplanationReason,
        call: ExplanationCall | None = None,
        summary: str | None = None,
        conclusion_count: int = 0,
        dropped_conclusion_count: int = 0,
        illegal_citation_count: int = 0,
    ) -> MatchExplanation:
        return MatchExplanation(
            id=uuid4(),
            run_id=run_id,
            application_id=application_id,
            candidate_profile_id=candidate_profile_id,
            status=status,
            reason_code=reason.value,
            summary=summary,
            model=call.model if call is not None else None,
            prompt_version=prompt_version,
            rule_version=rule_version,
            latency_ms=call.latency_ms if call is not None else 0,
            attempts=call.attempts if call is not None else 0,
            prompt_tokens=call.prompt_tokens if call is not None else None,
            completion_tokens=call.completion_tokens if call is not None else None,
            conclusion_count=conclusion_count,
            dropped_conclusion_count=dropped_conclusion_count,
            illegal_citation_count=illegal_citation_count,
        )


@dataclass
class _Counters:
    """Per-candidate tallies. Mutable because each verification step adds to them."""

    offered: int = 0
    kept: int = 0
    dropped: int = 0
    illegal: int = 0


__all__ = [
    "MODEL_CLAIM_TYPE",
    "MODEL_SUPPORT_CEILING",
    "EvidenceProvider",
    "ExplanationRunResult",
    "JobQueryProvider",
    "MatchExplanationService",
    "classify_failure",
    "locate_citation",
    "next_display_order",
]
