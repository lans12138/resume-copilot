"""Celery maintenance tasks: thin wrappers that build context and call services.

Per detailed design §14, the task only parses parameters, builds the worker's
resources, and invokes :class:`MaintenanceService`. The timeout sweep is
idempotent: a PENDING approval that already moved off PENDING (decided or expired
by a concurrent batch) is skipped, so at-least-once redelivery is safe.

Async note: the whole database/redis workflow runs inside a *single* event loop
(a single ``asyncio.run``). The async SQLAlchemy engine and the redis client are
both loop-bound, and Celery's prefork worker forks child processes after module
import — so we must not share a cached ``RuntimeResources`` across tasks, nor
split the work across several ``asyncio.run`` calls (which would attach the
connection to different loops and raise "attached to a different loop" /
"cannot use Connection.transaction() in a manually started transaction"). We
therefore build resources per task invocation and dispose them in the same loop.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from celery import Task  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.repository import SqlAgentRunRepository
from backend.app.agent.service import RunService
from backend.app.approvals.repository import SqlApprovalRepository
from backend.app.approvals.service import ApprovalService
from backend.app.auth.tokens import Actor
from backend.app.core.settings import get_settings
from backend.app.infrastructure.celery import app
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.job_applications.repository import (
    SqlApplicationRunRepository,
    SqlJobApplicationRepository,
)
from backend.app.maintenance.service import MaintenanceService


async def _authorize_noop(actor: Actor, job_id: UUID) -> None:
    """The sweep runs internally; no per-actor authorization is needed."""
    return None


async def _expire(
    resources: RuntimeResources, session: AsyncSession, before: datetime
) -> int:
    approval_repo = SqlApprovalRepository(session)
    arun_repo = SqlApplicationRunRepository(session)
    app_repo = SqlJobApplicationRepository(session)
    run_service = RunService(
        SqlAgentRunRepository(session),
        resources.checkpointer,
        notifier=resources.event_notifier,
    )
    approval_service = ApprovalService(
        authorize=_authorize_noop,
        approval_repo=approval_repo,
        arun_repo=arun_repo,
        app_repo=app_repo,
        run_service=run_service,
    )
    maintenance = MaintenanceService(
        approval_repo=approval_repo,
        arun_repo=arun_repo,
        app_repo=app_repo,
        run_service=run_service,
        approval_service=approval_service,
    )
    return await maintenance.expire_pending_approvals(before)


@app.task(name="maintenance.expire_approvals", bind=True)  # type: ignore[untyped-decorator]
def expire_approvals(self: Task, before_iso: str | None = None) -> dict[str, object]:
    """Promote timed-out PENDING approvals to EXPIRED (§11.8)."""
    resources = RuntimeResources.build(get_settings())
    before = datetime.fromisoformat(before_iso) if before_iso else datetime.now(UTC)

    async def _run() -> dict[str, object]:
        session = resources.session_factory()
        try:
            count = await _expire(resources, session, before)
            await session.commit()
            return {"status": "ok", "expired": count}
        finally:
            await session.close()
            # Tear down the loop-bound engine/redis within the same event loop so
            # no loop-bound connection survives into the next task's loop.
            await resources.close()

    return asyncio.run(_run())
