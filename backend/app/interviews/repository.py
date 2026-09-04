"""Interview persistence ports (IMP-024).

Both adapters key on ``approval_id`` so the execution service can ask "did this
approval already produce an interview?" before writing — the idempotency backstop
behind "execute exactly once" (§17.2).
"""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.interviews.models import Interview
from backend.app.job_applications.models import JobApplication


class InterviewRepository(Protocol):
    """Persistence contract for interviews."""

    async def save_interview(self, interview: Interview) -> None: ...

    async def get_by_approval(self, approval_id: UUID) -> Interview | None: ...

    async def get_by_external_id(self, external_schedule_id: str) -> Interview | None: ...

    async def get_by_run(self, run_id: UUID) -> Interview | None: ...

    async def list_by_job(self, job_id: UUID) -> list[Interview]: ...


class InMemoryInterviewRepository:
    """Lock-guarded store; keyed by approval_id and external_schedule_id."""

    def __init__(self) -> None:
        self._by_approval: dict[UUID, Interview] = {}
        self._by_external: dict[str, Interview] = {}
        self._by_run: dict[UUID, Interview] = {}
        self._lock = asyncio.Lock()

    async def save_interview(self, interview: Interview) -> None:
        async with self._lock:
            self._by_approval[interview.approval_id] = interview
            self._by_external[interview.external_schedule_id] = interview
            self._by_run[interview.run_id] = interview

    async def get_by_approval(self, approval_id: UUID) -> Interview | None:
        async with self._lock:
            return self._by_approval.get(approval_id)

    async def get_by_external_id(self, external_schedule_id: str) -> Interview | None:
        async with self._lock:
            return self._by_external.get(external_schedule_id)

    async def get_by_run(self, run_id: UUID) -> Interview | None:
        async with self._lock:
            return self._by_run.get(run_id)

    async def list_by_job(self, job_id: UUID) -> list[Interview]:
        # In-memory store has no application->job projection; SQL adapter joins.
        return []


class SqlInterviewRepository:
    """PostgreSQL adapter for interviews."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_interview(self, interview: Interview) -> None:
        self._session.add(interview)
        await self._session.flush()

    async def get_by_approval(self, approval_id: UUID) -> Interview | None:
        result = await self._session.execute(
            select(Interview).where(Interview.approval_id == approval_id)
        )
        return result.scalars().first()

    async def get_by_external_id(self, external_schedule_id: str) -> Interview | None:
        result = await self._session.execute(
            select(Interview).where(Interview.external_schedule_id == external_schedule_id)
        )
        return result.scalars().first()

    async def get_by_run(self, run_id: UUID) -> Interview | None:
        result = await self._session.execute(
            select(Interview).where(Interview.run_id == run_id)
        )
        return result.scalars().first()

    async def list_by_job(self, job_id: UUID) -> list[Interview]:
        result = await self._session.execute(
            select(Interview)
            .join(
                JobApplication,
                Interview.application_id == JobApplication.id,
            )
            .where(JobApplication.job_id == job_id)
            .order_by(Interview.created_at.desc())
        )
        return list(result.scalars().all())
