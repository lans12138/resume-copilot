"""Authenticated Job, version, lifecycle, and assignment endpoints."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.jobs.models import Job, JobAssignment, JobStatus, JobVersion
from backend.app.jobs.schemas import (
    AssignmentCreate,
    AssignmentListResponse,
    AssignmentResponse,
    JobCreate,
    JobListResponse,
    JobRequirements,
    JobResponse,
    JobUpdate,
    JobVersionResponse,
    VersionCommand,
)
from backend.app.jobs.service import JobService

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


def job_version_response(version: JobVersion) -> JobVersionResponse:
    return JobVersionResponse(
        id=version.id,
        version_no=version.version_no,
        description=version.description_text,
        requirements=JobRequirements.model_validate(version.requirements_json),
        content_sha256=version.content_sha256,
        schema_version=version.schema_version,
        created_at=version.created_at,
    )


async def job_response(service: JobService, job: Job) -> JobResponse:
    return JobResponse(
        id=job.id,
        title=job.title,
        status=job.status,
        version=job.version,
        created_by=job.created_by,
        created_at=job.created_at,
        updated_at=job.updated_at,
        current_version=job_version_response(await service.current_version(job)),
    )


def assignment_response(assignment: JobAssignment) -> AssignmentResponse:
    return AssignmentResponse(
        id=assignment.id,
        job_id=assignment.job_id,
        user_id=assignment.user_id,
        assigned_by=assignment.assigned_by,
        assigned_at=assignment.assigned_at,
        revoked_by=assignment.revoked_by,
        revoked_at=assignment.revoked_at,
    )


async def commit_job_change(service: JobService, *, duplicate_code: str) -> None:
    try:
        await service.session.commit()
    except StaleDataError as error:
        await service.session.rollback()
        raise AppError(
            code="VERSION_CONFLICT",
            http_status=409,
            safe_message="岗位已被其他请求更新",
        ) from error
    except IntegrityError as error:
        await service.session.rollback()
        raise AppError(
            code=duplicate_code,
            http_status=409,
            safe_message="请求与现有数据冲突",
        ) from error


@router.get("", response_model=JobListResponse)
async def list_jobs(
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: JobStatus | None = None,
) -> JobListResponse:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        service = JobService(session)
        jobs, total = await service.list_authorized(
            actor, page=page, page_size=page_size, status=status
        )
        items = [await job_response(service, job) for job in jobs]
        return JobListResponse(items=items, page=page, page_size=page_size, total=total)


@router.post("", response_model=JobResponse, status_code=201)
async def create_job(
    request: Request,
    payload: JobCreate,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> JobResponse:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        service = JobService(session)
        job = await service.create(actor, payload)
        await commit_job_change(service, duplicate_code="DUPLICATE_JOB_CONTENT")
        await session.refresh(job)
        return await job_response(service, job)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> JobResponse:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        service = JobService(session)
        return await job_response(service, await service.get_authorized(actor, job_id))


@router.patch("/{job_id}", response_model=JobResponse)
async def update_job(
    job_id: UUID,
    payload: JobUpdate,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> JobResponse:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        service = JobService(session)
        job = await service.update(actor, await service.get_authorized(actor, job_id), payload)
        await commit_job_change(service, duplicate_code="DUPLICATE_JOB_CONTENT")
        await session.refresh(job)
        return await job_response(service, job)


async def transition_job(
    job_id: UUID,
    payload: VersionCommand,
    request: Request,
    actor: Actor,
    target: JobStatus,
) -> JobResponse:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        service = JobService(session)
        job = await service.transition(
            actor,
            await service.get_authorized(actor, job_id),
            target,
            payload.expected_version,
        )
        await commit_job_change(service, duplicate_code="INVALID_STATE")
        await session.refresh(job)
        return await job_response(service, job)


@router.post("/{job_id}/activate", response_model=JobResponse)
async def activate_job(
    job_id: UUID,
    payload: VersionCommand,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> JobResponse:
    return await transition_job(job_id, payload, request, actor, JobStatus.ACTIVE)


@router.post("/{job_id}/close", response_model=JobResponse)
async def close_job(
    job_id: UUID,
    payload: VersionCommand,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> JobResponse:
    return await transition_job(job_id, payload, request, actor, JobStatus.CLOSED)


@router.get("/{job_id}/assignments", response_model=AssignmentListResponse)
async def list_assignments(
    job_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AssignmentListResponse:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        service = JobService(session)
        job = await service.get_authorized(actor, job_id)
        assignments = await service.list_assignments(actor, job)
        start = (page - 1) * page_size
        return AssignmentListResponse(
            items=[assignment_response(item) for item in assignments[start : start + page_size]],
            page=page,
            page_size=page_size,
            total=len(assignments),
        )


@router.post("/{job_id}/assignments", response_model=AssignmentResponse, status_code=201)
async def grant_assignment(
    job_id: UUID,
    payload: AssignmentCreate,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> AssignmentResponse:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        service = JobService(session)
        assignment = await service.grant_assignment(
            actor,
            await service.get_authorized(actor, job_id),
            payload.user_id,
        )
        await commit_job_change(service, duplicate_code="ASSIGNMENT_EXISTS")
        return assignment_response(assignment)


@router.delete("/{job_id}/assignments/{user_id}", status_code=204)
async def revoke_assignment(
    job_id: UUID,
    user_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> Response:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        service = JobService(session)
        await service.revoke_assignment(
            actor,
            await service.get_authorized(actor, job_id),
            user_id,
        )
        await commit_job_change(service, duplicate_code="ASSIGNMENT_NOT_ACTIVE")
    return Response(status_code=204)
