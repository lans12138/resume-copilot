"""Pydantic response schemas for MatchRun reports (IMP-020)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.app.candidates.summaries import CandidateDisplaySummary
from backend.app.reports.models import ReportView


class EvidenceOut(BaseModel):
    """One verbatim excerpt backing a claim."""

    model_config = ConfigDict(from_attributes=True)

    evidence_chunk_id: UUID
    quote_text: str
    quote_start: int
    quote_end: int


class ClaimOut(BaseModel):
    """One conclusion with its (validated) evidence.

    ``source`` tells a reader whether the claim is a deterministic verdict
    (``RULE``) or model commentary (``MODEL``). It is part of the response rather
    than something the client infers from ``claim_type``, because the two carry
    different authority (BR-001 / BR-002) and a client that had to pattern-match
    a string prefix to tell them apart would eventually get it wrong.
    """

    model_config = ConfigDict(from_attributes=True)

    claim_type: str
    claim_text: str
    source: str
    impact_level: str
    support_level: str
    confidence_note: str | None = None
    display_order: int
    evidences: list[EvidenceOut]


class ReportOut(BaseModel):
    """One candidate's scored, evidence-backed MatchRun report."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    application_id: UUID
    candidate_profile_id: UUID
    overall_score: float
    recommendation: str
    summary: str
    model_snapshot_json: dict[str, object]
    created_at: datetime
    claims: list[ClaimOut]
    # PORT-005: the report heading names the candidate, and the evidence panel
    # deep-links to the original text. Both are read live from the profile, so both
    # are optional — a report whose profile can no longer be read still has to be
    # readable, and the client falls back to the profile id.
    display_name: str | None = None
    document_id: UUID | None = None


class ReportList(BaseModel):
    """All reports persisted for a single MatchRun."""

    reports: list[ReportOut]

    @classmethod
    def from_views(
        cls,
        views: list[ReportView],
        summaries: Mapping[UUID, CandidateDisplaySummary] | None = None,
    ) -> ReportList:
        """Build the response, optionally naming each report's candidate.

        ``summaries`` is optional so an existing caller that only needs the scored
        claims does not have to fabricate a lookup; the fields then stay null and the
        client degrades to the profile id instead of failing.
        """
        resolved = summaries or {}
        return cls(
            reports=[
                _report_out(view, resolved.get(view.report.candidate_profile_id))
                for view in views
            ]
        )


def _report_out(
    view: ReportView, summary: CandidateDisplaySummary | None = None
) -> ReportOut:
    return ReportOut(
        id=view.report.id,
        run_id=view.report.run_id,
        application_id=view.report.application_id,
        candidate_profile_id=view.report.candidate_profile_id,
        overall_score=view.report.overall_score,
        recommendation=view.report.recommendation.value,
        summary=view.report.summary,
        model_snapshot_json=view.report.model_snapshot_json,
        created_at=view.report.created_at,
        display_name=summary.display_name if summary else None,
        document_id=summary.document_id if summary else None,
        claims=[
            ClaimOut(
                claim_type=claim_view.claim.claim_type,
                claim_text=claim_view.claim.claim_text,
                source=claim_view.claim.source.value,
                impact_level=claim_view.claim.impact_level.value,
                support_level=claim_view.claim.support_level.value,
                confidence_note=claim_view.claim.confidence_note,
                display_order=claim_view.claim.display_order,
                evidences=[
                    EvidenceOut(
                        evidence_chunk_id=evidence.evidence_chunk_id,
                        quote_text=evidence.quote_text,
                        quote_start=evidence.quote_start,
                        quote_end=evidence.quote_end,
                    )
                    for evidence in claim_view.evidences
                ],
            )
            for claim_view in view.claims
        ],
    )
