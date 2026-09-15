"""Approval API schemas (IMP-022)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from backend.app.approvals.models import ApprovalActionType, ApprovalStatus
from backend.app.approvals.service import DecisionAction


class ApprovalDetail(BaseModel):
    """Read model for a single approval decision (§12.5)."""

    id: UUID
    application_run_id: UUID
    action_type: ApprovalActionType
    status: ApprovalStatus
    original_params: dict[str, Any] | None = Field(default=None, alias="original_params")
    final_params: dict[str, Any] | None = Field(default=None, alias="final_params")
    expected_application_version: int | None
    idempotency_key: str
    version: int
    expires_at: datetime | None
    decided_by: UUID | None
    decided_at: datetime | None

    @classmethod
    def from_approval(cls, approval: Any) -> ApprovalDetail:
        return cls(
            id=approval.id,
            application_run_id=approval.application_run_id,
            action_type=approval.action_type,
            status=approval.status,
            original_params=approval.original_params_json,
            final_params=approval.final_params_json,
            expected_application_version=approval.expected_application_version,
            idempotency_key=approval.idempotency_key,
            version=approval.version,
            expires_at=approval.expires_at,
            decided_by=approval.decided_by,
            decided_at=approval.decided_at,
        )

    model_config = {"populate_by_name": True}


class DecisionRequest(BaseModel):
    """Body of ``POST /approvals/{id}/decision`` (§12.5)."""

    decision: DecisionAction
    expected_version: int
    edited_params: dict[str, Any] | None = None
