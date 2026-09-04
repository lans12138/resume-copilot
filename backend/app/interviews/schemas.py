"""Interview question and schedule schemas (IMP-024, detailed design §11.7).

Every type here is a Pydantic model so the API layer can validate and serialize
it, and so the graph nodes can build typed, bounded data instead of free text.
Question wording is *exploratory* by default: the model is never allowed to
assert a protected attribute about the candidate (see ``sensitivity``).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

# Schema version written to ApplicationRun.question_schema_version (§4.5).
QUESTION_SCHEMA_VERSION = "v1"


class InterviewQuestion(BaseModel):
    """One structured interview question (§11.7)."""

    question_id: str
    question_text: str
    competency: str
    rationale: str
    # Evidence chunk ids backing the question; empty when no evidence was found.
    evidence_chunk_ids: list[UUID] = Field(default_factory=list)
    # Sensitivity flags raised by the guard (e.g. a protected attribute surfaced).
    sensitivity_flags: list[str] = Field(default_factory=list)


class InterviewQuestionSet(BaseModel):
    """Versioned set of interview questions generated after SHORTLISTED (§11.7)."""

    questions: list[InterviewQuestion] = Field(default_factory=list)
    schema_version: str = QUESTION_SCHEMA_VERSION


class ScheduleStatus(StrEnum):
    """Lifecycle of a (mock) schedule record."""

    SCHEDULED = "SCHEDULED"
    FAILED = "FAILED"


class ScheduleProposal(BaseModel):
    """What the schedule backend should create (§11.7, line 965).

    MVP keeps no real external contact; ``interviewer_label`` is a free-text label
    ("Hiring Manager") and ``candidate_slots`` are opaque candidate-preferred times.
    """

    application_id: UUID
    duration_minutes: int = 45
    timezone: str = "UTC"
    candidate_slots: list[dict[str, Any]] = Field(default_factory=list)
    interviewer_label: str = "Hiring Manager"


class ScheduleResult(BaseModel):
    """Outbound schedule confirmation returned by the backend (§11.7)."""

    external_schedule_id: str
    status: ScheduleStatus = ScheduleStatus.SCHEDULED
    application_id: UUID
    created_at: datetime
    proposal: ScheduleProposal


class CreateInterviewScheduleCommand(BaseModel):
    """Executor input for the CREATE_INTERVIEW_SCHEDULE side effect (§11.7).

    Only these fields cross the boundary; the executing service re-reads and
    re-validates the ``Approval``, ``Run`` and ``Application`` and the actor's
    permission. The model never supplies job_id, actor role, or version — those
    come from persisted business state (§11.6, line 957).
    """

    application_id: UUID
    run_id: UUID
    approval_id: UUID
    actor_id: UUID
    idempotency_key: str
    proposal: ScheduleProposal
