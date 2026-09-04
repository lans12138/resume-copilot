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

from backend.app.agent.checkpoint import InMemoryCheckpointer
from backend.app.agent.repository import SqlAgentRunRepository
from backend.app.agent.service import RunService
from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.core.errors import app_error
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
from backend.app.jobs.service import JobService
from backend.app.reports.models import MatchReport

router = APIRouter(prefix="/api/v1", tags=["application-runs"])


async def application_run_service(request: Request) -> AsyncGenerator[ApplicationRunService, None]:
    """Build the service per request from process resources (yield = DI scope)."""
    resources = request.app.state.resources
    async with resources.session_factory() as session:
        agent_repo = SqlAgentRunRepository(session)
        run_service = RunService(agent_repo, InMemoryCheckpointer())
        app_repo = SqlJobApplicationRepository(session)
        arun_repo = SqlApplicationRunRepository(session)

        async def authorize(actor: Actor, job_id: UUID) -> None:
            await JobService(session).get_authorized(actor, job_id)

        async def report_lookup(report_id: UUID) -> Any | None:
            return await session.get(MatchReport, report_id)

        yield ApplicationRunService(
            app_repo=app_repo,
            arun_repo=arun_repo,
            run_service=run_service,
            authorize=authorize,
            report_lookup=report_lookup,
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
