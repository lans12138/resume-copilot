"""Approval API endpoints (IMP-022).

``GET /approvals/{id}`` and ``POST /approvals/{id}/decision`` are the human gate
into an ApplicationRun's side effects (detailed design §12.5). Both use the same
transactional ApplicationRun wiring as the run endpoints, so a decision, its
business mutation, graph resume/checkpoint, next approval, and terminal cleanup
commit atomically.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from backend.app.approvals.schemas import ApprovalDetail, DecisionRequest
from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.idempotency.dependency import IdempotencyGuardDep
from backend.app.job_applications.service import ApplicationRunService
from backend.app.job_applications.wiring import application_run_service

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


ServiceDep = Annotated[
    ApplicationRunService, Depends(application_run_service, scope="function")
]
ActorDep = Annotated[Actor, Depends(get_current_actor)]


@router.get("/{approval_id}", response_model=ApprovalDetail)
async def get_approval(
    approval_id: UUID,
    actor: ActorDep,
    service: ServiceDep,
) -> ApprovalDetail:
    approval = await service.get_approval_for_actor(actor, approval_id)
    return ApprovalDetail.from_approval(approval)


@router.post("/{approval_id}/decision", response_model=ApprovalDetail)
async def decide_approval(
    approval_id: UUID,
    body: DecisionRequest,
    actor: ActorDep,
    service: ServiceDep,
    request: Request,
    guard: IdempotencyGuardDep,
) -> ApprovalDetail:
    # The ``Idempotency-Key`` header is enforced at the request level here (FIN-001):
    # an identical retried request replays the first response; a different request
    # under the same key is rejected with 409 IDEMPOTENCY_KEY_REUSED.
    await service.decide_approval(
        actor,
        approval_id,
        body.decision,
        expected_version=body.expected_version,
        edited_params=body.edited_params,
    )
    approval = await service.get_approval_for_actor(actor, approval_id)
    detail = ApprovalDetail.from_approval(approval)
    await guard.complete(200, detail.model_dump(mode="json"), resource_id=str(approval.id))
    return detail
