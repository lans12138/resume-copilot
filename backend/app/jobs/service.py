"""Job versioning, lifecycle, assignment, and resource authorization."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.models import User, UserRole
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError
from backend.app.jobs.models import Job, JobAssignment, JobStatus, JobVersion
from backend.app.jobs.schemas import JobCreate, JobRequirements, JobUpdate

JOB_TRANSITIONS = {
    JobStatus.DRAFT: {JobStatus.ACTIVE},
    JobStatus.ACTIVE: {JobStatus.CLOSED},
    JobStatus.CLOSED: set(),
}


def content_hash(description: str, requirements: JobRequirements) -> str:
    canonical = json.dumps(
        {"description": description, "requirements": requirements.model_dump(mode="json")},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def require_transition(current: JobStatus, target: JobStatus) -> None:
    if target not in JOB_TRANSITIONS[current]:
        raise AppError(code="INVALID_STATE", http_status=409, safe_message="岗位状态不允许该操作")


def require_hr(actor: Actor) -> None:
    if actor.role is not UserRole.HR:
        raise AppError(code="FORBIDDEN", http_status=403, safe_message="当前用户无岗位管理权限")


class JobService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, actor: Actor, payload: JobCreate) -> Job:
        require_hr(actor)
        job = Job(title=payload.title, status=JobStatus.DRAFT, created_by=actor.user_id)
        self.session.add(job)
        await self.session.flush()
        version = self._new_version(job, actor, payload.description, payload.requirements, 1)
        self.session.add(version)
        await self.session.flush()
        job.current_version_id = version.id
        return job

    async def update(self, actor: Actor, job: Job, payload: JobUpdate) -> Job:
        require_hr(actor)
        self._require_version(job, payload.expected_version)
        if job.status is JobStatus.CLOSED:
            raise AppError(code="INVALID_STATE", http_status=409, safe_message="已关闭岗位不可编辑")
        current = await self.current_version(job)
        changed = False
        if payload.title is not None:
            changed = changed or payload.title != job.title
            job.title = payload.title
        description = payload.description or current.description_text
        requirements = payload.requirements or JobRequirements.model_validate(
            current.requirements_json
        )
        digest = content_hash(description, requirements)
        if digest != current.content_sha256:
            next_version = self._new_version(
                job, actor, description, requirements, current.version_no + 1
            )
            self.session.add(next_version)
            await self.session.flush()
            job.current_version_id = next_version.id
            changed = True
        if changed:
            job.version += 1
        return job

    async def transition(
        self, actor: Actor, job: Job, target: JobStatus, expected_version: int
    ) -> Job:
        require_hr(actor)
        self._require_version(job, expected_version)
        require_transition(job.status, target)
        job.status = target
        job.version += 1
        return job

    async def get_authorized(self, actor: Actor, job_id: UUID) -> Job:
        job = await self.session.get(Job, job_id)
        if job is None:
            raise self._not_found()
        if actor.role is UserRole.HR:
            return job
        if actor.role is UserRole.HIRING_MANAGER and await self.has_active_assignment(
            actor.user_id, job_id
        ):
            return job
        raise self._not_found()

    async def list_authorized(
        self,
        actor: Actor,
        *,
        page: int,
        page_size: int,
        status: JobStatus | None,
    ) -> tuple[list[Job], int]:
        if actor.role is UserRole.ADMIN:
            raise AppError(code="FORBIDDEN", http_status=403, safe_message="管理员无招聘业务权限")
        query = select(Job)
        if actor.role is UserRole.HIRING_MANAGER:
            query = query.join(JobAssignment).where(
                JobAssignment.user_id == actor.user_id,
                JobAssignment.revoked_at.is_(None),
            )
        if status is not None:
            query = query.where(Job.status == status)
        count = await self.session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await self.session.scalars(
            query.order_by(Job.created_at.desc(), Job.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows), int(count or 0)

    async def current_version(self, job: Job) -> JobVersion:
        if job.current_version_id is None:
            raise RuntimeError("job has no current version")
        version = await self.session.get(JobVersion, job.current_version_id)
        if version is None:
            raise RuntimeError("job current version is missing")
        return version

    async def grant_assignment(self, actor: Actor, job: Job, user_id: UUID) -> JobAssignment:
        require_hr(actor)
        target = await self.session.get(User, user_id)
        if target is None or not target.is_active or target.role is not UserRole.HIRING_MANAGER:
            raise AppError(
                code="INVALID_ASSIGNEE",
                http_status=422,
                safe_message="只能分配有效的招聘主管",
            )
        existing = await self.session.scalar(
            select(JobAssignment).where(
                JobAssignment.job_id == job.id,
                JobAssignment.user_id == user_id,
                JobAssignment.revoked_at.is_(None),
            )
        )
        if existing is not None:
            raise AppError(code="ASSIGNMENT_EXISTS", http_status=409, safe_message="有效分配已存在")
        assignment = JobAssignment(job_id=job.id, user_id=user_id, assigned_by=actor.user_id)
        self.session.add(assignment)
        await self.session.flush()
        return assignment

    async def revoke_assignment(self, actor: Actor, job: Job, user_id: UUID) -> None:
        require_hr(actor)
        revoked_id = await self.session.scalar(
            update(JobAssignment)
            .where(
                JobAssignment.job_id == job.id,
                JobAssignment.user_id == user_id,
                JobAssignment.revoked_at.is_(None),
            )
            .values(revoked_by=actor.user_id, revoked_at=datetime.now(UTC))
            .returning(JobAssignment.id)
        )
        if revoked_id is None:
            raise AppError(
                code="ASSIGNMENT_NOT_ACTIVE",
                http_status=409,
                safe_message="没有可撤销的有效分配",
            )

    async def list_assignments(self, actor: Actor, job: Job) -> list[JobAssignment]:
        if actor.role is UserRole.HR:
            query = select(JobAssignment).where(JobAssignment.job_id == job.id)
        elif actor.role is UserRole.HIRING_MANAGER:
            query = select(JobAssignment).where(
                JobAssignment.job_id == job.id,
                JobAssignment.user_id == actor.user_id,
                JobAssignment.revoked_at.is_(None),
            )
        else:
            raise AppError(code="FORBIDDEN", http_status=403, safe_message="无岗位分配查看权限")
        return list(await self.session.scalars(query.order_by(JobAssignment.assigned_at.desc())))

    async def has_active_assignment(self, user_id: UUID, job_id: UUID) -> bool:
        assignment_id = await self.session.scalar(
            select(JobAssignment.id).where(
                JobAssignment.user_id == user_id,
                JobAssignment.job_id == job_id,
                JobAssignment.revoked_at.is_(None),
            )
        )
        return assignment_id is not None

    def _new_version(
        self,
        job: Job,
        actor: Actor,
        description: str,
        requirements: JobRequirements,
        version_no: int,
    ) -> JobVersion:
        return JobVersion(
            job_id=job.id,
            version_no=version_no,
            description_text=description,
            requirements_json=requirements.model_dump(mode="json"),
            content_sha256=content_hash(description, requirements),
            schema_version="v1",
            created_by=actor.user_id,
        )

    @staticmethod
    def _require_version(job: Job, expected: int) -> None:
        if job.version != expected:
            raise AppError(
                code="VERSION_CONFLICT",
                http_status=409,
                safe_message="岗位已被其他请求更新",
                details={"current_version": job.version},
            )

    @staticmethod
    def _not_found() -> AppError:
        return AppError(code="JOB_NOT_FOUND", http_status=404, safe_message="岗位不存在")
