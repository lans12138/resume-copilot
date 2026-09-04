"""Job application aggregate and ApplicationRun child (IMP-021).

A ``JobApplication`` is one candidate's application to a job. Its
``active_application_run_id`` column is the *exclusive slot* that guarantees at
most one non-terminal ``ApplicationRun`` per application (detailed design
§4.5/§11): the partial unique index plus the composite FK to ``application_runs``
keep the active run provably bound to the application, and the create transaction
claims it atomically (``claim_active_run`` returns 409 if already occupied).

``ApplicationRun`` is the APPLICATION-kind child of ``agent_runs`` (its ``run_id``
is the primary key and a FK to ``agent_runs.id``). It carries only the
per-application decision context: which report triggered the flow, the completion
reason, and the generated interview questions. The lifecycle status lives on the
parent ``AgentRun``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database import Base


class ApplicationStatus(StrEnum):
    """Lifecycle of a candidate's application to a job (§4.5)."""

    CREATED = "CREATED"
    SHORTLISTED = "SHORTLISTED"
    ON_HOLD = "ON_HOLD"
    REJECTED = "REJECTED"
    INTERVIEW_SCHEDULED = "INTERVIEW_SCHEDULED"


class JobApplication(Base):
    """A candidate's application to a job, owning the active-run exclusive slot."""

    __tablename__ = "job_applications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('CREATED','SHORTLISTED','ON_HOLD','REJECTED','INTERVIEW_SCHEDULED')",
            name="ck_job_applications_status",
        ),
        UniqueConstraint("job_id", "candidate_id", name="uq_job_applications_job_candidate"),
        Index("ix_job_applications_job_status", "job_id", "status"),
        # Exclusive slot: at most one non-null active run per application.
        Index(
            "uq_job_applications_active_run",
            "active_application_run_id",
            unique=True,
            postgresql_where=text("active_application_run_id IS NOT NULL"),
        ),
        # The active run must belong to *this* application (§4.5). This FK targets
        # application_runs, which is created later, so it is deferred.
        ForeignKeyConstraint(
            ["active_application_run_id", "id"],
            ["application_runs.run_id", "application_runs.application_id"],
            name="fk_job_applications_active_run",
            use_alter=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("candidates.id"), nullable=False)
    status: Mapped[ApplicationStatus] = mapped_column(
        String(32), default=ApplicationStatus.CREATED, nullable=False
    )
    # Exclusive slot; null when no ApplicationRun is active.
    active_application_run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # Optimistic-lock version for slot claim / clear (manually advanced).
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ApplicationRun(Base):
    """APPLICATION-kind child of ``agent_runs`` (the decision context)."""

    __tablename__ = "application_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id"], ["agent_runs.id"], name="fk_application_runs_run"
        ),
        ForeignKeyConstraint(
            ["application_id"], ["job_applications.id"], name="fk_application_runs_application"
        ),
        # Composite identity referenced by the job_applications active slot.
        UniqueConstraint("run_id", "application_id", name="uq_application_runs_run_application"),
        Index("ix_application_runs_application", "application_id"),
    )

    run_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    application_id: Mapped[UUID] = mapped_column(nullable=False)
    # Report that triggered this flow (match_reports FK, added in IMP-030).
    match_report_id: Mapped[UUID | None] = mapped_column(nullable=True)
    completion_reason: Mapped[str | None] = mapped_column(String(64))
    question_set_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    question_schema_version: Mapped[str | None] = mapped_column(String(32))


class ApplicationStatusHistory(Base):
    """Append-only audit of JobApplication.status transitions (§11.6).

    Every side-effecting status change is recorded here inside the same
    transaction that updates the application and executes the approval, so the
    history, the new status, and the executed approval are mutually consistent
    (§11.6, line 959). It is never updated in place.
    """

    __tablename__ = "application_status_history"
    __table_args__ = (
        Index("ix_application_status_history_application", "application_id"),
        Index("ix_application_status_history_approval", "approval_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    application_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_applications.id"), nullable=False
    )
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    # Actor who decided the approval that caused the change (audit only).
    changed_by: Mapped[UUID | None] = mapped_column(nullable=True)
    # Run and approval that drove the change; null for manual edits.
    run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    approval_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # Bounded, safe reason (never free text from untrusted documents).
    safe_reason: Mapped[str | None] = mapped_column(String(256))
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
