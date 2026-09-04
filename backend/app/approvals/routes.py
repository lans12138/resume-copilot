"""Approval API endpoints (IMP-022).

``GET /approvals/{id}`` and ``POST /approvals/{id}/decision`` are the human gate
into an ApplicationRun's side effects (detailed design §12.5). The endpoint only
assembles the SQL adapters and the ``ApprovalService`` from the shared session and
process checkpointer; all decision logic (decide-once, optimistic lock, run
migration) lives in the service. ``Idempotency-Key`` is accepted per the contract
and consumed by the idempotency framework added later; the approval state machine
already guarantees "decide exactly once" for IMP-022.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.repository import SqlAgentRunRepository
from backend.app.agent.service import RunService
from backend.app.approvals.repository import SqlApprovalRepository
from backend.app.approvals.schemas import ApprovalDetail, DecisionRequest
from backend.app.approvals.service import ApprovalService
from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.job_applications.repository import (
    SqlApplicationRunRepository,
    SqlJobApplicationRepository,
)
from backend.app.jobs.service import JobService

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


def _build_service(session: AsyncSession, resources: RuntimeResources) -> ApprovalService:
    agent_repo = SqlAgentRunRepository(session)
    run_service = RunService(agent_repo, resources.checkpointer)
    approval_repo = SqlApprovalRepository(session)
    arun_repo = SqlApplicationRunRepository(session)
    app_repo = SqlJobApplicationRepository(session)

    async def authorize(actor: Actor, job_id: UUID) -> None:
        await JobService(session).get_authorized(actor, job_id)

    return ApprovalService(
        authorize=authorize,
        approval_repo=approval_repo,
        arun_repo=arun_repo,
        app_repo=app_repo,
        run_service=run_service,
    )


@router.get("/{approval_id}", response_model=ApprovalDetail)
async def get_approval(
    approval_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> ApprovalDetail:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        service = _build_service(session, resources)
        approval = await service.get_approval_for_actor(actor, approval_id)
        return ApprovalDetail.from_approval(approval)


@router.post("/{approval_id}/decision", response_model=ApprovalDetail)
async def decide_approval(
    approval_id: UUID,
    body: DecisionRequest,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ApprovalDetail:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        service = _build_service(session, resources)
        approval, _status = await service.decide(
            actor,
            approval_id,
            body.decision,
            expected_version=body.expected_version,
            edited_params=body.edited_params,
            idempotency_key=idempotency_key,
        )
        return ApprovalDetail.from_approval(approval)
