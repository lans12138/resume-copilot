"""Pydantic response schemas for match explanations (PORT-003).

One row per candidate report, carrying the *call record* rather than the
conclusions: the conclusions are already in the report as ``MODEL`` claims, and
duplicating them here would give a client two places to read the same sentence
from. What this exposes is the part the report cannot show — whether the model
ran at all, and if not, why (``reason_code``), under which model and prompt
version, and how much retry budget it spent.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.app.explanations.models import MatchExplanation


class ExplanationOut(BaseModel):
    """The model call record for one candidate's report."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    application_id: UUID
    candidate_profile_id: UUID
    status: str
    reason_code: str
    summary: str | None = None
    model: str | None = None
    prompt_version: str
    rule_version: str
    latency_ms: int
    attempts: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    conclusion_count: int
    dropped_conclusion_count: int
    illegal_citation_count: int
    created_at: datetime


class ExplanationList(BaseModel):
    """Every explanation record persisted for a single MatchRun."""

    explanations: list[ExplanationOut]

    @classmethod
    def from_rows(cls, rows: list[MatchExplanation]) -> ExplanationList:
        return cls(
            explanations=[ExplanationOut.model_validate(row) for row in rows]
        )


__all__ = ["ExplanationList", "ExplanationOut"]
