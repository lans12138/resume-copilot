"""Interview aggregate (IMP-024, detailed design §4.5/§11.7).

An ``Interview`` is the concrete artefact produced by the second approval's side
effect (CREATE_INTERVIEW_SCHEDULE). Its ``external_schedule_id`` is the stable id
returned by the schedule backend, and its ``approval_id`` is the foreign key that
binds the interview to the *executed* approval so a resumed run can tell whether
the side effect already happened (§17.2, line 1272: "已执行副作用根据 Approval
idempotency_key 查询，不盲目重复").
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database import Base


class InterviewStatus(StrEnum):
    """Lifecycle of a scheduled interview."""

    SCHEDULED = "SCHEDULED"
    CANCELLED = "CANCELLED"


class Interview(Base):
    """One mock-scheduled interview created after the second approval."""

    __tablename__ = "interviews"
    __table_args__ = (
        CheckConstraint(
            "status IN ('SCHEDULED','CANCELLED')", name="ck_interviews_status"
        ),
        # The external id from the schedule backend is unique: a re-executed side
        # effect returns the same id and never creates a second interview.
        CheckConstraint(
            "external_schedule_id IS NOT NULL", name="ck_interviews_external_id"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    application_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_applications.id"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    approval_id: Mapped[UUID] = mapped_column(nullable=False)
    external_schedule_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    schedule_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[InterviewStatus] = mapped_column(
        String(32), default=InterviewStatus.SCHEDULED, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
