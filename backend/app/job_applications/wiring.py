"""Transactional SQL wiring shared by ApplicationRun and Approval routes."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.checkpoint import SqlCheckpointer
from backend.app.agent.enqueuer import CeleryRunEnqueuer, RunEnqueuer
from backend.app.agent.repository import SqlAgentRunRepository
from backend.app.agent.service import RunService
from backend.app.approvals.repository import SqlApprovalRepository
from backend.app.approvals.service import ApprovalService
from backend.app.auth.tokens import Actor
from backend.app.infrastructure.celery import app as celery_app
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.interviews.repository import SqlInterviewRepository
from backend.app.interviews.schedule import MockScheduleBackend
from backend.app.job_applications.repository import (
    SqlApplicationRunRepository,
    SqlJobApplicationRepository,
)
from backend.app.job_applications.service import ApplicationRunService
from backend.app.job_applications.side_effects import ApplicationSideEffectService
from backend.app.jobs.service import JobService
from backend.app.reports.models import MatchReport


def build_application_run_service(
    session: AsyncSession,
    resources: RuntimeResources,
    *,
    enqueuer: RunEnqueuer | None = None,
) -> ApplicationRunService:
    """Build the complete workflow over one database transaction."""
    agent_repo = SqlAgentRunRepository(session)
    run_service = RunService(
        agent_repo,
        SqlCheckpointer(session),
        notifier=resources.event_notifier,
    )
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
    side_effects = ApplicationSideEffectService(
        approval_service=approval_service,
        app_repo=app_repo,
        arun_repo=arun_repo,
        run_service=run_service,
        interview_repo=SqlInterviewRepository(session),
        schedule_backend=MockScheduleBackend(),
    )
    return ApplicationRunService(
        app_repo=app_repo,
        arun_repo=arun_repo,
        run_service=run_service,
        authorize=authorize,
        approval_service=approval_service,
        report_lookup=report_lookup,
        side_effects=side_effects,
        enqueuer=enqueuer,
        defer_execution=enqueuer is not None,
    )


async def application_run_service(
    request: Request,
) -> AsyncGenerator[ApplicationRunService, None]:
    """Yield a workflow service and atomically commit all resulting facts."""
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        try:
            service = build_application_run_service(
                session, resources, enqueuer=CeleryRunEnqueuer(celery_app)
            )
            yield service
            await session.commit()
            service.publish_pending_deliveries()
        except BaseException:
            await session.rollback()
            raise
