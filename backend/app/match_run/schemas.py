"""MatchRun API schemas (IMP-026, detailed design §12.4)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CreateMatchRunRequest(BaseModel):
    """Body of ``POST /jobs/{id}/match-runs`` (config overrides optional)."""

    rule_version: str = "v1"
    prompt_version: str = "v1"
    retrieval_config: dict[str, object] | None = None
    model_config_override: dict[str, object] | None = None


class MatchRunAccepted(BaseModel):
    """202 response: the created run and its initial (non-terminal) status."""

    run_id: UUID
    job_id: UUID
    status: str


class MatchRunCandidateOut(BaseModel):
    """One frozen candidate ranking inside a MatchRun.

    The ranking is frozen at run time, but the *display* fields are read live from
    the profile they point at (PORT-005): a presenter connects a ranking row to its
    report and its approval by name, and an eight-character profile id is not a
    name. They are optional rather than required because a row whose profile can no
    longer be read must still render — the ranking is the record, the name is a
    courtesy, and a 500 over a missing name would lose the record.
    """

    candidate_profile_id: UUID
    application_id: UUID
    snapshot_order: int
    rrf_score: float
    processing_status: str
    hard_rule_overall: str | None
    display_name: str | None = None
    normalized_skills: list[str] = []
    years_experience: float | None = None
    education_level: str | None = None
    # The source document, so the row can deep-link to the candidate's original text.
    document_id: UUID | None = None


class MatchRunSummary(BaseModel):
    """One row in the per-job MatchRun list."""

    run_id: UUID
    job_id: UUID
    job_version_id: UUID
    status: str
    attempt: int
    created_at: datetime


class MatchRunDetail(BaseModel):
    """Single MatchRun view (run-level status + frozen candidate snapshots)."""

    run_id: UUID
    job_id: UUID
    job_version_id: UUID
    status: str
    attempt: int
    rule_version: str
    prompt_version: str
    created_at: datetime
    finished_at: datetime | None
    candidates: list[MatchRunCandidateOut]


class MatchRunList(BaseModel):
    """All MatchRuns for a job."""

    runs: list[MatchRunSummary]
