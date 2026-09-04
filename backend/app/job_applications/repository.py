"""JobApplication and ApplicationRun persistence ports (IMP-021).

The repository owns the one non-obvious concurrency rule of the ApplicationRun
active slot (detailed design §4.5/§11): claiming the slot is an *atomic
check-and-set*. ``claim_active_run`` must return 409 the moment the slot is
already occupied, and two concurrent claimers must end with exactly one success.
The in-memory adapter uses a lock; the SQL adapter uses a conditional
``UPDATE ... WHERE active_application_run_id IS NULL RETURNING`` that takes a row
lock, so the database — not application code — decides the winner.

``clear_active_run`` only clears the slot when it *still* points at the run that
is entering a terminal state. A stale attempt must never wipe a newer run's slot
(§11, last bullet).
"""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError, app_error
from backend.app.job_applications.models import ApplicationRun, JobApplication


def _not_found() -> AppError:
    return app_error("APPLICATION_NOT_FOUND", http_status=404, safe_message="投递不存在")


class JobApplicationRepository(Protocol):
    """Persistence contract for the application aggregate and its active slot."""

    async def save_application(self, application: JobApplication) -> None: ...

    async def get_application(self, application_id: UUID) -> JobApplication | None: ...

    async def claim_active_run(self, application_id: UUID, run_id: UUID) -> None:
        """Atomically set the slot; raise APPLICATION_RUN_ALREADY_ACTIVE/409 if taken."""
        ...

    async def clear_active_run(self, application_id: UUID, expected_run_id: UUID) -> None:
        """Clear the slot only if it still points at ``expected_run_id``."""
        ...


class ApplicationRunRepository(Protocol):
    """Persistence contract for ApplicationRun child rows."""

    async def save_application_run(self, run: ApplicationRun) -> None: ...

    async def get_application_run(self, run_id: UUID) -> ApplicationRun | None: ...

    async def list_by_application(self, application_id: UUID) -> list[ApplicationRun]: ...


class InMemoryJobApplicationRepository:
    """Lock-guarded store; slot claim/clear are atomic under concurrency."""

    def __init__(self) -> None:
        self._apps: dict[UUID, JobApplication] = {}
        self._lock = asyncio.Lock()

    async def save_application(self, application: JobApplication) -> None:
        async with self._lock:
            self._apps[application.id] = application

    async def get_application(self, application_id: UUID) -> JobApplication | None:
        async with self._lock:
            return self._apps.get(application_id)

    async def claim_active_run(self, application_id: UUID, run_id: UUID) -> None:
        async with self._lock:
            app = self._apps.get(application_id)
            if app is None:
                raise _not_found()
            if app.active_application_run_id is not None:
                raise app_error(
                    "APPLICATION_RUN_ALREADY_ACTIVE",
                    http_status=409,
                    safe_message="该投递已有进行中的流程",
                    details={"current_run_id": str(app.active_application_run_id)},
                )
            app.active_application_run_id = run_id
            app.version += 1

    async def clear_active_run(self, application_id: UUID, expected_run_id: UUID) -> None:
        async with self._lock:
            app = self._apps.get(application_id)
            if app is None:
                return
            if app.active_application_run_id == expected_run_id:
                app.active_application_run_id = None
                app.version += 1


class SqlJobApplicationRepository:
    """PostgreSQL adapter; the slot claim is a conditional row-locked UPDATE."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_application(self, application: JobApplication) -> None:
        self._session.add(application)
        await self._session.flush()

    async def get_application(self, application_id: UUID) -> JobApplication | None:
        return await self._session.get(JobApplication, application_id)

    async def claim_active_run(self, application_id: UUID, run_id: UUID) -> None:
        result = await self._session.execute(
            text(
                "UPDATE job_applications "
                "SET active_application_run_id = :rid, version = version + 1 "
                "WHERE id = :aid AND active_application_run_id IS NULL "
                "RETURNING id"
            ),
            {"rid": run_id, "aid": application_id},
        )
        if result.first() is None:
            app = await self._session.get(JobApplication, application_id)
            if app is None:
                raise _not_found()
            raise app_error(
                "APPLICATION_RUN_ALREADY_ACTIVE",
                http_status=409,
                safe_message="该投递已有进行中的流程",
                details={"current_run_id": str(app.active_application_run_id)},
            )
        await self._session.flush()

    async def clear_active_run(self, application_id: UUID, expected_run_id: UUID) -> None:
        await self._session.execute(
            text(
                "UPDATE job_applications "
                "SET active_application_run_id = NULL, version = version + 1 "
                "WHERE id = :aid AND active_application_run_id = :rid"
            ),
            {"aid": application_id, "rid": expected_run_id},
        )
        await self._session.flush()


class InMemoryApplicationRunRepository:
    """Lock-guarded in-process store for ApplicationRun child rows."""

    def __init__(self) -> None:
        self._runs: dict[UUID, ApplicationRun] = {}
        self._lock = asyncio.Lock()

    async def save_application_run(self, run: ApplicationRun) -> None:
        async with self._lock:
            self._runs[run.run_id] = run

    async def get_application_run(self, run_id: UUID) -> ApplicationRun | None:
        async with self._lock:
            return self._runs.get(run_id)

    async def list_by_application(self, application_id: UUID) -> list[ApplicationRun]:
        async with self._lock:
            return [r for r in self._runs.values() if r.application_id == application_id]


class SqlApplicationRunRepository:
    """PostgreSQL adapter for ApplicationRun child rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_application_run(self, run: ApplicationRun) -> None:
        self._session.add(run)
        await self._session.flush()

    async def get_application_run(self, run_id: UUID) -> ApplicationRun | None:
        return await self._session.get(ApplicationRun, run_id)

    async def list_by_application(self, application_id: UUID) -> list[ApplicationRun]:
        result = await self._session.execute(
            select(ApplicationRun).where(ApplicationRun.application_id == application_id)
        )
        return list(result.scalars().all())
