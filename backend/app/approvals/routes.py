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

from fastapi import APIRouter, Depends

from backend.app.approvals.schemas import ApprovalDetail, DecisionRequest
from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
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
) -> ApprovalDetail:
    # Idempotency is enforced at the business level by ``ApprovalService.decide``:
    # a non-PENDING approval is rejected with 409 APPROVAL_ALREADY_DECIDED, and an
    # optimistic version guards concurrent editors. Side effects are applied exactly
    # once by ``ApplicationSideEffectService`` keyed on ``approval.idempotency_key``.
    # FIN-001 request-level replay is intentionally NOT applied here: it would
    # short-circuit that 409 guard and mask already-decided approvals behind a 200.
    await service.decide_approval(
        actor,
        approval_id,
        body.decision,
        expected_version=body.expected_version,
        edited_params=body.edited_params,
    )
    approval = await service.get_approval_for_actor(actor, approval_id)
    return ApprovalDetail.from_approval(approval)
