"""Approval aggregate (IMP-022).

An ``Approval`` is the human-decision record that gates every side-effecting
node of an ``ApplicationRun`` (detailed design §4.5/§5.5/§11). It is created in
``PENDING`` when the run pauses at ``human_review`` (§11.4), decided exactly
once (§11.5), and only then may the run resume and apply a side effect (§11.6/§11.7,
added in IMP-024). The ``status`` machine is the source of truth for "decided
once": a second decision call is rejected because the approval is no longer
``PENDING``.

Constraints mirror §4.5: a partial unique index allows at most one ``PENDING``
approval per run (prevents duplicate proposals on node replay); a global unique
index on ``idempotency_key`` dedupes the ``run_id:attempt:action_type:ordinal``
business key; and ``ix_approvals_pending_expiry`` lets the maintenance sweeper
(IMP-023) find timed-out approvals cheaply.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
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


class ApprovalStatus(StrEnum):
    """Lifecycle of a single approval decision (§5.5)."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    EDITED = "EDITED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXPIRED = "EXPIRED"


class ApprovalActionType(StrEnum):
    """Side effect the approval gates (§4.5)."""

    UPDATE_APPLICATION_STATUS = "UPDATE_APPLICATION_STATUS"
    CREATE_INTERVIEW_SCHEDULE = "CREATE_INTERVIEW_SCHEDULE"


class Approval(Base):
    """One human decision that gates a side-effecting node of an ApplicationRun."""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','APPROVED','EDITED','REJECTED','EXECUTED',"
            "'EXECUTION_FAILED','EXPIRED')",
            name="ck_approvals_status",
        ),
        CheckConstraint(
            "action_type IN ('UPDATE_APPLICATION_STATUS','CREATE_INTERVIEW_SCHEDULE')",
            name="ck_approvals_action_type",
        ),
        UniqueConstraint("idempotency_key", name="uq_approvals_idempotency_key"),
        UniqueConstraint("id", "application_run_id", name="uq_approvals_id_run"),
        # At most one PENDING approval per run (node replay returns the existing one).
        Index(
            "uq_approvals_pending_run",
            "application_run_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_approvals_pending_expiry", "status", "expires_at"),
        ForeignKeyConstraint(
            ["application_run_id"],
            ["application_runs.run_id"],
            name="fk_approvals_application_run",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    application_run_id: Mapped[UUID] = mapped_column(nullable=False)
    action_type: Mapped[ApprovalActionType] = mapped_column(
        Enum(
            ApprovalActionType,
            native_enum=False,
            create_constraint=False,
            length=64,
        ),
        default=ApprovalActionType.UPDATE_APPLICATION_STATUS,
        nullable=False,
    )
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(
            ApprovalStatus,
            native_enum=False,
            create_constraint=False,
            length=32,
        ),
        default=ApprovalStatus.PENDING,
        nullable=False,
    )
    # Agent proposal, frozen at pause time (§11.4).
    original_params_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Approved or edited parameters; null until decided.
    final_params_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Application version the side effect must be applied against (§11.4).
    expected_application_version: Mapped[int | None] = mapped_column(Integer)
    # Business idempotency key: run_id:attempt:action_type:ordinal (§11.4).
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Optimistic-lock version; bumped on every decision (§11.5).
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiration_reason: Mapped[str | None] = mapped_column(String(32))
    decided_by: Mapped[UUID | None] = mapped_column(nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    execution_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
