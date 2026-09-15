"""Interview API endpoints (IMP-026, detailed design §12.5).

Interviews are created as the side effect of the second approval
(CREATE_INTERVIEW_SCHEDULE, IMP-024). These read-only endpoints let the browser
show the scheduled interview (and its proposal) from the ApplicationRun or
Interview page. Authorization is the same job-level check used everywhere else.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.core.errors import app_error
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.interviews.models import Interview
from backend.app.interviews.repository import SqlInterviewRepository
from backend.app.interviews.schemas import InterviewDetail, InterviewList
from backend.app.job_applications.models import JobApplication
from backend.app.jobs.service import JobService

router = APIRouter(prefix="/api/v1/interviews", tags=["interviews"])


ActorDep = Annotated[Actor, Depends(get_current_actor)]


@router.get("/{interview_id}", response_model=InterviewDetail)
async def get_interview(
    interview_id: UUID, actor: ActorDep, request: Request
) -> InterviewDetail:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        interview = await session.get(Interview, interview_id)
        if interview is None:
            raise app_error("INTERVIEW_NOT_FOUND", http_status=404, safe_message="面试不存在")
        job_id = await _job_of_application(session, interview.application_id)
        await JobService(session).get_authorized(actor, job_id)
        return InterviewDetail.from_interview(interview)


@router.get("", response_model=InterviewList)
async def list_interviews(
    job_id: UUID, actor: ActorDep, request: Request
) -> InterviewList:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        await JobService(session).get_authorized(actor, job_id)
        interviews = await SqlInterviewRepository(session).list_by_job(job_id)
        return InterviewList.from_interviews(interviews)


async def _job_of_application(session: AsyncSession, application_id: UUID) -> UUID:
    application = await session.get(JobApplication, application_id)
    if application is None:
        raise app_error("APPLICATION_NOT_FOUND", http_status=404, safe_message="投递不存在")
    return application.job_id
