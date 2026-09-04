"""ApplicationRun API (IMP-021).

Endpoints (detailed design §12.5):

* ``POST /applications/{id}/runs`` — claim the active slot and start the flow;
  returns 202 ``RunAccepted`` or 409 ``APPLICATION_RUN_ALREADY_ACTIVE`` with the
  current run id when the slot is taken.
* ``GET /applications/{id}/runs`` — list the application's runs (history allowed).
* ``GET /application-runs/{id}`` — single run detail.

Approval, decision, cancel, and resume endpoints land in IMP-022/023. The
checkpointer used here is in-process (``InMemoryCheckpointer``); IMP-030 swaps it
for an ``AsyncPostgresSaver`` without touching the service or graph.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from backend.app.agent.repository import SqlAgentRunRepository
from backend.app.agent.service import RunService
from backend.app.approvals.repository import SqlApprovalRepository
from backend.app.approvals.service import ApprovalService
from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.core.errors import app_error
from backend.app.interviews.repository import InMemoryInterviewRepository
from backend.app.interviews.schedule import MockScheduleBackend
from backend.app.job_applications.repository import (
    SqlApplicationRunRepository,
    SqlJobApplicationRepository,
)
from backend.app.job_applications.schemas import (
    ApplicationRunDetail,
    ApplicationRunSummary,
    CreateRunRequest,
    RunAccepted,
)
from backend.app.job_applications.service import ApplicationRunService
from backend.app.job_applications.side_effects import ApplicationSideEffectService
from backend.app.jobs.service import JobService
from backend.app.reports.models import MatchReport

router = APIRouter(prefix="/api/v1", tags=["application-runs"])


async def application_run_service(request: Request) -> AsyncGenerator[ApplicationRunService, None]:
    """Build the service per request from process resources (yield = DI scope)."""
    resources = request.app.state.resources
    async with resources.session_factory() as session:
        agent_repo = SqlAgentRunRepository(session)
        # Shared process checkpointer so a WAITING_APPROVAL run resumes across
        # requests (the real PG saver arrives in IMP-030, §17.2).
        run_service = RunService(agent_repo, resources.checkpointer)
        app_repo = SqlJobApplicationRepository(session)
        arun_repo = SqlApplicationRunRepository(session)
        approval_repo = SqlApprovalRepository(session)

        async def authorize(actor: Actor, job_id: UUID) -> None:
            await JobService(session).get_authorized(actor, job_id)

        async def report_lookup(report_id: UUID) -> Any | None:
            return await session.get(MatchReport, report_id)

        approval_service = ApprovalService(
            authorize=authorize,
            approval_repo=approval_repo,
            arun_repo=arun_repo,
            app_repo=app_repo,
            run_service=run_service,
        )
        # MVP schedule backend: in-process MockScheduleBackend, idempotent on the
        # approval key. A real calendar would swap this for an Outbox-backed client
        # (§11.7). The interview store is per-request here; production shares the
        # session-scoped repository (IMP-030 wires PG).
        interview_repo = InMemoryInterviewRepository()
        schedule_backend = MockScheduleBackend()
        side_effects = ApplicationSideEffectService(
            approval_service=approval_service,
            app_repo=app_repo,
            arun_repo=arun_repo,
            run_service=run_service,
            interview_repo=interview_repo,
            schedule_backend=schedule_backend,
        )
        yield ApplicationRunService(
            app_repo=app_repo,
            arun_repo=arun_repo,
            run_service=run_service,
            authorize=authorize,
            approval_service=approval_service,
            report_lookup=report_lookup,
            side_effects=side_effects,
        )


ServiceDep = Annotated[ApplicationRunService, Depends(application_run_service)]
ActorDep = Annotated[Actor, Depends(get_current_actor)]


@router.post("/applications/{application_id}/runs", status_code=202, response_model=RunAccepted)
async def create_application_run(
    application_id: UUID,
    payload: CreateRunRequest,
    actor: ActorDep,
    service: ServiceDep,
) -> RunAccepted:
    agent_run, application_run = await service.create_application_run(
        actor, application_id, payload.match_report_id
    )
    return RunAccepted(
        run_id=agent_run.id,
        application_id=application_run.application_id,
        status=agent_run.status.value,
    )


@router.get("/applications/{application_id}/runs", response_model=list[ApplicationRunSummary])
async def list_application_runs(
    application_id: UUID,
    actor: ActorDep,
    service: ServiceDep,
) -> list[ApplicationRunSummary]:
    runs = await service.list_runs(application_id)
    return [
        ApplicationRunSummary(
            run_id=agent_run.id,
            application_id=application_run.application_id,
            status=agent_run.status.value,
            attempt=agent_run.attempt,
            match_report_id=application_run.match_report_id,
        )
        for agent_run, application_run in runs
    ]


@router.get("/application-runs/{run_id}", response_model=ApplicationRunDetail)
async def get_application_run(
    run_id: UUID,
    actor: ActorDep,
    service: ServiceDep,
) -> ApplicationRunDetail:
    found = await service.get_application_run(run_id)
    if found is None:
        raise app_error("APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")
    agent_run, application_run = found
    return ApplicationRunDetail(
        run_id=agent_run.id,
        application_id=application_run.application_id,
        status=agent_run.status.value,
        attempt=agent_run.attempt,
        completion_reason=application_run.completion_reason,
        match_report_id=application_run.match_report_id,
    )
