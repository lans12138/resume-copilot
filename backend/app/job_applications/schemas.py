"""Pydantic contracts for the ApplicationRun API (IMP-021)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel

from backend.app.approvals.schemas import ApprovalDetail


class CreateRunRequest(BaseModel):
    """Body for ``POST /applications/{id}/runs``."""

    match_report_id: UUID | None = None


class RunAccepted(BaseModel):
    """202 response: the created run and its initial (non-terminal) status."""

    run_id: UUID
    application_id: UUID
    status: str


class ApplicationRunSummary(BaseModel):
    """One row in the per-application run list."""

    run_id: UUID
    application_id: UUID
    status: str
    attempt: int
    match_report_id: UUID | None = None


class ApplicationRunDetail(BaseModel):
    """Single run view (IMP-026 adds question set, approval, interview)."""

    run_id: UUID
    application_id: UUID
    status: str
    attempt: int
    completion_reason: str | None = None
    match_report_id: UUID | None = None
    question_set: dict[str, Any] | None = None
    current_approval: ApprovalDetail | None = None
    interview_external_id: str | None = None
    interview_status: str | None = None
    interview_id: UUID | None = None
