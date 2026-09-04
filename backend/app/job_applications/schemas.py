"""Pydantic contracts for the ApplicationRun API (IMP-021)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel


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
    """Single run view; the current PENDING Approval is added in IMP-022."""

    run_id: UUID
    application_id: UUID
    status: str
    attempt: int
    completion_reason: str | None = None
    match_report_id: UUID | None = None
