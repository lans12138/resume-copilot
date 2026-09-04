"""Celery maintenance tasks: thin wrappers that build context and call services.

Per detailed design §14, the task only parses parameters, builds the worker's
resources, and invokes :class:`MaintenanceService`. The timeout sweep is
idempotent: a PENDING approval that already moved off PENDING (decided or expired
by a concurrent batch) is skipped, so at-least-once redelivery is safe.
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

_cached_resources: RuntimeResources | None = None


def _resources() -> RuntimeResources:
    global _cached_resources
    if _cached_resources is None:
        _cached_resources = RuntimeResources.build(get_settings())
    return _cached_resources


async def _authorize_noop(actor: Actor, job_id: UUID) -> None:
    """The sweep runs internally; no per-actor authorization is needed."""
    return None


async def _expire(session: AsyncSession, before: datetime) -> int:
    approval_repo = SqlApprovalRepository(session)
    arun_repo = SqlApplicationRunRepository(session)
    app_repo = SqlJobApplicationRepository(session)
    run_service = RunService(SqlAgentRunRepository(session), _resources().checkpointer)
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
    resources = _resources()
    session = resources.session_factory()
    before = datetime.fromisoformat(before_iso) if before_iso else datetime.now(UTC)
    try:
        count = asyncio.run(_expire(session, before))
        asyncio.run(session.commit())
        return {"status": "ok", "expired": count}
    finally:
        asyncio.run(session.close())
