"""MatchRun report generation and persistence (IMP-020).

``ReportService`` turns each ``COMPLETED`` ``MatchRunCandidate`` into an
evidence-backed ``MatchReport``. Report construction is a **pure** function
(``build_candidate_report``) so it is unit-testable without IO; persistence and
evidence lookup are the only IO boundaries. Before anything is written, every
evidence reference is verified against the candidate's own ``evidence_chunks``
(§9.4): an illegal reference is dropped and counted, never persisted. The gate
metric "illegal evidence references == 0" therefore holds whenever the builder
only cites real, owned excerpts — which it always does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID, uuid4

from backend.app.agent.models import AgentRun
from backend.app.candidates.models import EvidenceChunk
from backend.app.match_run.models import MatchRun, MatchRunCandidate, ProcessingStatus
from backend.app.reports.models import (
    ClaimEvidence,
    ClaimView,
    ImpactLevel,
    MatchReport,
    Recommendation,
    ReportClaim,
    ReportView,
    SupportLevel,
)
from backend.app.reports.repository import ReportRepository
from backend.app.reports.validation import (
    apply_high_impact_guard,
    verify_evidence_reference,
)
from backend.app.retrieval.models import HardRuleOutcome


class EvidenceProvider(Protocol):
    """Supplies the verbatim chunks a candidate's claims may cite (§4.5)."""

    async def list_chunks(self, candidate_profile_id: UUID) -> list[EvidenceChunk]: ...


@dataclass(frozen=True)
class ReportBuildResult:
    """Outcome of generating reports for one MatchRun."""

    candidates_total: int
    reports_written: int
    illegal_reference_count: int


def _parse_hard_rule(
    candidate: MatchRunCandidate,
) -> tuple[HardRuleOutcome, list[dict[str, object]]]:
    """Reconstruct the hard-rule verdict from the snapshot JSON column."""
    data: dict[str, object] = candidate.hard_rule_result_json or {
        "overall": "UNKNOWN",
        "rules": [],
    }
    overall = HardRuleOutcome(cast(str, data.get("overall", "UNKNOWN")))
    raw_rules = data.get("rules")
    rules = cast("list[dict[str, object]]", raw_rules) if isinstance(raw_rules, list) else []
    return overall, rules


def _score_and_recommendation(
    *, snapshot_order: int, overall: HardRuleOutcome
) -> tuple[float, Recommendation]:
    """Deterministic, reproducible overall score and tier (no model call)."""
    rank_score = max(0, 100 - (snapshot_order - 1) * 5)
    hard_adjust = {HardRuleOutcome.PASS: 0, HardRuleOutcome.FAIL: -40, HardRuleOutcome.UNKNOWN: -20}
    overall_score = max(0.0, min(100.0, float(rank_score + hard_adjust[overall])))
    if overall == HardRuleOutcome.FAIL:
        recommendation = Recommendation.WEAK_MATCH
    elif overall == HardRuleOutcome.UNKNOWN:
        recommendation = Recommendation.REVIEW
    elif overall_score >= 80:
        recommendation = Recommendation.STRONG_MATCH
    else:
        recommendation = Recommendation.MATCH
    return round(overall_score, 2), recommendation


def _support_for(outcome: HardRuleOutcome) -> SupportLevel:
    if outcome == HardRuleOutcome.PASS:
        return SupportLevel.SUPPORTED
    return SupportLevel.INSUFFICIENT


def _impact_for(outcome: HardRuleOutcome) -> ImpactLevel:
    # A failing hard rule is the highest-impact hiring signal.
    return ImpactLevel.HIGH if outcome == HardRuleOutcome.FAIL else ImpactLevel.MEDIUM


def _quote_evidence(chunk: EvidenceChunk, claim_id: UUID) -> ClaimEvidence:
    """Cite the whole chunk verbatim — a legal, owned reference by construction."""
    return ClaimEvidence(
        id=uuid4(),
        claim_id=claim_id,
        evidence_chunk_id=chunk.id,
        quote_text=chunk.text,
        quote_start=0,
        quote_end=len(chunk.text),
    )


def build_candidate_report(
    *,
    candidate: MatchRunCandidate,
    chunks: list[EvidenceChunk],
    match_run: MatchRun,
    application_id: UUID,
) -> ReportView:
    """Pure: assemble one candidate's ``ReportView`` from frozen run data.

    Every deterministic SUPPORTED claim cites a real, owned chunk; a high-impact
    claim that is not SUPPORTED is rewritten with the "insufficient evidence"
    template (§4.5). When a candidate has no chunks, no deterministic claim is
    emitted (only the scored report), so there is never an unbacked verdict.
    """
    overall, rules = _parse_hard_rule(candidate)
    overall_score, recommendation = _score_and_recommendation(
        snapshot_order=candidate.snapshot_order, overall=overall
    )
    summary = (
        f"候选人综合评分 {overall_score:.2f} 分（满分 100）；"
        f"硬性条件结论：{overall.value}；快照排名：第 {candidate.snapshot_order} 位。"
    )
    report = MatchReport(
        id=uuid4(),
        run_id=match_run.run_id,
        application_id=application_id,
        candidate_profile_id=candidate.candidate_profile_id,
        overall_score=overall_score,
        recommendation=recommendation,
        summary=summary,
        model_snapshot_json={
            "model": match_run.model_config_json,
            "prompt_version": match_run.prompt_version,
            "rule_version": match_run.rule_version,
        },
    )

    primary_chunk = chunks[0] if chunks else None
    claims: list[ReportClaim] = []

    # Claim 1 — aggregate hard-rule verdict.
    overall_claim_id = uuid4()
    overall_support = _support_for(overall)
    overall_impact = _impact_for(overall)
    overall_text = (
        "候选人满足全部硬性条件，可进入下一轮" if overall == HardRuleOutcome.PASS
        else "硬性条件证据不足，无法判定是否满足"
    )
    overall_text, overall_support = apply_high_impact_guard(
        impact_level=overall_impact, support_level=overall_support, claim_text=overall_text
    )
    claims.append(
        ReportClaim(
            id=overall_claim_id,
            report_id=report.id,
            claim_type="hard_rule_overall",
            claim_text=overall_text,
            impact_level=overall_impact,
            support_level=overall_support,
            confidence_note=f"overall={overall.value}",
            display_order=0,
        )
    )

    # Claims 2..n — one per individual hard rule.
    for index, rule in enumerate(rules, start=1):
        rule_id = str(rule.get("rule_id", f"rule_{index}"))
        rule_outcome = HardRuleOutcome(cast(str, rule.get("result", "UNKNOWN")))
        rule_claim_id = uuid4()
        rule_support = _support_for(rule_outcome)
        rule_impact = _impact_for(rule_outcome)
        observed = rule.get("observed_value")
        required = rule.get("required_value")
        rule_text = (
            f"满足{rule_id}（观测值 {observed}，要求 {required}）"
            if rule_outcome == HardRuleOutcome.PASS
            else f"无法满足{rule_id}（观测值 {observed}，要求 {required}）"
        )
        rule_text, rule_support = apply_high_impact_guard(
            impact_level=rule_impact, support_level=rule_support, claim_text=rule_text
        )
        claims.append(
            ReportClaim(
                id=rule_claim_id,
                report_id=report.id,
                claim_type=f"hard_rule:{rule_id}",
                claim_text=rule_text,
                impact_level=rule_impact,
                support_level=rule_support,
                confidence_note=f"result={rule_outcome.value}",
                display_order=index,
            )
        )

    # Wire evidence: cite the primary chunk for every claim that has one.
    claim_views: list[ClaimView] = []
    for claim in claims:
        evidences: list[ClaimEvidence] = []
        if primary_chunk is not None and claim.support_level == SupportLevel.SUPPORTED:
            # Only SUPPORTED claims assert a backed verdict; INSUFFICIENT claims
            # carry no verbatim assertion to pin (they already say "insufficient").
            evidences.append(_quote_evidence(primary_chunk, claim.id))
        claim_views.append(ClaimView(claim=claim, evidences=evidences))
    return ReportView(report=report, claims=claim_views)


class ReportService:
    """Persist evidence-backed reports for a MatchRun's completed candidates."""

    def __init__(self, repository: ReportRepository) -> None:
        self._reports = repository

    async def generate_for_run(
        self,
        *,
        run: AgentRun,
        match_run: MatchRun,
        candidates: list[MatchRunCandidate],
        evidence: EvidenceProvider,
    ) -> ReportBuildResult:
        """Build, verify, and persist one report per ``COMPLETED`` candidate.

        The run's reports are *replaced*, not appended to: they are derived from this
        scoring pass, so re-running the pass (a §5.6 retry, or an at-least-once
        redelivery that got this far) must leave one attempt's conclusions rather
        than two sets colliding on ``uq_match_reports_run_application``.
        """
        await self._reports.delete_by_run(run.id)
        total = 0
        written = 0
        illegal = 0
        for candidate in candidates:
            if candidate.processing_status != ProcessingStatus.COMPLETED:
                continue
            total += 1
            chunks = await evidence.list_chunks(candidate.candidate_profile_id)
            view = build_candidate_report(
                candidate=candidate,
                chunks=chunks,
                match_run=match_run,
                application_id=candidate.application_id,
            )
            chunk_by_id = {chunk.id: chunk for chunk in chunks}
            # §9.4: verify every evidence reference; drop illegals, count them.
            legal_views: list[ClaimView] = []
            for claim_view in view.claims:
                legal_evidences: list[ClaimEvidence] = []
                for evidence_row in claim_view.evidences:
                    chunk = chunk_by_id.get(evidence_row.evidence_chunk_id)
                    verdict = verify_evidence_reference(
                        evidence=evidence_row,
                        chunk=chunk,
                        candidate_profile_id=candidate.candidate_profile_id,
                    )
                    if verdict.legal:
                        legal_evidences.append(evidence_row)
                    else:
                        illegal += 1
                legal_views.append(ClaimView(claim=claim_view.claim, evidences=legal_evidences))

            await self._reports.save_report(view.report)
            for claim_view in legal_views:
                await self._reports.save_claim(claim_view.claim)
                for evidence_row in claim_view.evidences:
                    await self._reports.save_evidence(evidence_row)
            written += 1
        return ReportBuildResult(
            candidates_total=total, reports_written=written, illegal_reference_count=illegal
        )
