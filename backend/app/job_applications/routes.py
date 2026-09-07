"""ApplicationRun API (IMP-021).

Endpoints (detailed design §12.5):

* ``POST /applications/{id}/runs`` — claim the active slot and start the flow;
  returns 202 ``RunAccepted`` or 409 ``APPLICATION_RUN_ALREADY_ACTIVE`` with the
  current run id when the slot is taken.
* ``GET /applications/{id}/runs`` — list the application's runs (history allowed).
* ``GET /application-runs/{id}`` — single run detail.

Approval decisions, side effects, checkpoints, and interviews share a PostgreSQL
transaction assembled by ``job_applications.wiring``.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from backend.app.approvals.schemas import ApprovalDetail
from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.idempotency.dependency import IdempotencyGuardDep
from backend.app.interviews.repository import SqlInterviewRepository
from backend.app.job_applications.repository import (
    SqlJobApplicationRepository,
)
from backend.app.job_applications.schemas import (
    ApplicationRunDetail,
    ApplicationRunSummary,
    CreateRunRequest,
    RunAccepted,
)
from backend.app.job_applications.service import ApplicationRunService
from backend.app.job_applications.wiring import application_run_service
from backend.app.jobs.service import JobService

router = APIRouter(prefix="/api/v1", tags=["application-runs"])


ServiceDep = Annotated[
    ApplicationRunService, Depends(application_run_service, scope="function")
]
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
    request: Request,
) -> ApplicationRunDetail:
    found = await service.get_application_run_detail(run_id)
    agent_run, application_run, pending = found
    interview_external_id: str | None = None
    interview_status: str | None = None
    interview_id: UUID | None = None
    resources = request.app.state.resources
    async with resources.session_factory() as session:
        interview = await SqlInterviewRepository(session).get_by_run(run_id)
        if interview is not None:
            interview_external_id = interview.external_schedule_id
            interview_status = interview.status.value
            interview_id = interview.id
    return ApplicationRunDetail(
        run_id=agent_run.id,
        application_id=application_run.application_id,
        status=agent_run.status.value,
        attempt=agent_run.attempt,
        completion_reason=application_run.completion_reason,
        match_report_id=application_run.match_report_id,
        question_set=application_run.question_set_json,
        current_approval=(
            ApprovalDetail.from_approval(pending) if pending is not None else None
        ),
        interview_external_id=interview_external_id,
        interview_status=interview_status,
        interview_id=interview_id,
    )


@router.post("/application-runs/{run_id}/retry", status_code=202, response_model=RunAccepted)
async def retry_application_run(
    run_id: UUID,
    actor: ActorDep,
    service: ServiceDep,
    guard: IdempotencyGuardDep,
) -> RunAccepted:
    agent_run, application_run = await service.retry_application_run(actor, run_id)
    result = RunAccepted(
        run_id=agent_run.id,
        application_id=application_run.application_id,
        status=agent_run.status.value,
    )
    await guard.complete(202, result.model_dump(mode="json"), resource_id=str(run_id))
    return result


@router.post("/application-runs/{run_id}/cancel", response_model=RunAccepted)
async def cancel_application_run(
    run_id: UUID,
    actor: ActorDep,
    service: ServiceDep,
    guard: IdempotencyGuardDep,
) -> RunAccepted:
    agent_run, application_run = await service.cancel_application_run(actor, run_id)
    result = RunAccepted(
        run_id=agent_run.id,
        application_id=application_run.application_id,
        status=agent_run.status.value,
    )
    await guard.complete(202, result.model_dump(mode="json"), resource_id=str(run_id))
    return result


@router.get("/jobs/{job_id}/applications", response_model=list[ApplicationRunSummary])
async def list_job_applications(
    job_id: UUID,
    actor: ActorDep,
    service: ServiceDep,
    request: Request,
) -> list[ApplicationRunSummary]:
    resources = request.app.state.resources
    async with resources.session_factory() as session:
        await JobService(session).get_authorized(actor, job_id)
        applications = await SqlJobApplicationRepository(session).list_by_job(job_id)
    summaries: list[ApplicationRunSummary] = []
    for application in applications:
        runs = await service.list_runs(application.id)
        for agent_run, application_run in runs:
            summaries.append(
                ApplicationRunSummary(
                    run_id=agent_run.id,
                    application_id=application_run.application_id,
                    status=agent_run.status.value,
                    attempt=agent_run.attempt,
                    match_report_id=application_run.match_report_id,
                )
            )
    return summaries
