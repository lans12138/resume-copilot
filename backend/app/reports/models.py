"""Evidence-backed MatchRun report models (IMP-020).

A ``MatchReport`` is the per-candidate decision artifact a MatchRun persists for
every ``COMPLETED`` candidate. It is made of deterministic ``ReportClaim`` rows,
each optionally pinned to verbatim ``ClaimEvidence`` pulled from the candidate's
``evidence_chunks`` (detailed design §4.5). The claim/evidence pair is the
"evidence chain first-class citizen" the architecture requires: no deterministic
conclusion may ship without a legal reference, and a high-impact claim that is
not ``SUPPORTED`` must be rewritten with the "insufficient evidence" template
(§4.5, last bullet).

The persistence rows intentionally carry *no* ORM relationships — the
``ReportRepository`` assembles the read-side ``ReportView`` hierarchy itself so
the in-memory and SQL adapters stay behaviour-identical (IMP-019 precedent).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database import Base


class Recommendation(StrEnum):
    """Four-tier MatchRun recommendation (§4.5)."""

    STRONG_MATCH = "STRONG_MATCH"
    MATCH = "MATCH"
    REVIEW = "REVIEW"
    WEAK_MATCH = "WEAK_MATCH"


class ImpactLevel(StrEnum):
    """How consequential a claim is to the hiring decision."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class SupportLevel(StrEnum):
    """Verdict on whether evidence semantically supports a claim (§9.4)."""

    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"


class MatchReport(Base):
    """One candidate's scored, evidence-backed decision for a MatchRun."""

    __tablename__ = "match_reports"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id"], ["match_runs.run_id"], name="fk_match_reports_run"
        ),
        # application_id targets job_applications (built in IMP-021); until then
        # it is a plain uuid column so the report stays hermetic.
        UniqueConstraint(
            "run_id", "application_id", name="uq_match_reports_run_application"
        ),
        Index("ix_match_reports_run", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    application_id: Mapped[UUID] = mapped_column(nullable=False)
    candidate_profile_id: Mapped[UUID] = mapped_column(nullable=False)
    overall_score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    recommendation: Mapped[Recommendation] = mapped_column(
        Enum(
            Recommendation,
            native_enum=False,
            create_constraint=False,
            length=32,
        ),
        nullable=False,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    model_snapshot_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReportClaim(Base):
    """A single deterministic conclusion inside a ``MatchReport``."""

    __tablename__ = "report_claims"
    __table_args__ = (
        ForeignKeyConstraint(
            ["report_id"], ["match_reports.id"], name="fk_report_claims_report"
        ),
        UniqueConstraint("report_id", "display_order", name="uq_report_claims_order"),
        Index("ix_report_claims_report", "report_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    report_id: Mapped[UUID] = mapped_column(nullable=False)
    claim_type: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    impact_level: Mapped[ImpactLevel] = mapped_column(
        Enum(
            ImpactLevel,
            native_enum=False,
            create_constraint=False,
            length=16,
        ),
        nullable=False,
    )
    support_level: Mapped[SupportLevel] = mapped_column(
        Enum(
            SupportLevel,
            native_enum=False,
            create_constraint=False,
            length=16,
        ),
        nullable=False,
    )
    confidence_note: Mapped[str | None] = mapped_column(Text)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)


class ClaimEvidence(Base):
    """A verbatim, source-pinned excerpt backing one ``ReportClaim``."""

    __tablename__ = "claim_evidences"
    __table_args__ = (
        ForeignKeyConstraint(
            ["claim_id"], ["report_claims.id"], name="fk_claim_evidences_claim"
        ),
        ForeignKeyConstraint(
            ["evidence_chunk_id"],
            ["evidence_chunks.id"],
            name="fk_claim_evidences_chunk",
        ),
        UniqueConstraint(
            "claim_id",
            "evidence_chunk_id",
            "quote_start",
            "quote_end",
            name="uq_claim_evidences_claim_chunk_range",
        ),
        CheckConstraint("quote_start >= 0", name="ck_claim_evidences_start_nonneg"),
        CheckConstraint("quote_end > quote_start", name="ck_claim_evidences_end_gt_start"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    claim_id: Mapped[UUID] = mapped_column(nullable=False)
    evidence_chunk_id: Mapped[UUID] = mapped_column(nullable=False)
    quote_text: Mapped[str] = mapped_column(Text, nullable=False)
    quote_start: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_end: Mapped[int] = mapped_column(Integer, nullable=False)


@dataclass(frozen=True)
class ClaimView:
    """A claim together with its (already validated) evidence excerpts."""

    claim: ReportClaim
    evidences: list[ClaimEvidence]


@dataclass(frozen=True)
class ReportView:
    """Read-side hierarchy a report endpoint / UI consumes."""

    report: MatchReport
    claims: list[ClaimView]
