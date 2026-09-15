"""Celery maintenance tasks: thin wrappers that build context and call services.

Per detailed design §14, a task only parses parameters, builds the worker's
resources, and invokes a service in :mod:`backend.app.maintenance.service`. All
lifecycle logic lives in that service. Every task here is *reconciliation*, so
at-least-once delivery is the expected mode rather than an edge case: a batch
delivered twice converges because each transition re-checks durable state before
applying it.

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
from datetime import UTC, datetime, timedelta
from uuid import UUID

from celery import Task, shared_task  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.enqueuer import CeleryRunEnqueuer
from backend.app.agent.repository import SqlAgentRunRepository
from backend.app.agent.service import RunService
from backend.app.approvals.repository import SqlApprovalRepository
from backend.app.approvals.service import ApprovalService
from backend.app.auth.tokens import Actor
from backend.app.core.settings import get_settings
from backend.app.documents.parse_service import CeleryParseEnqueuer
from backend.app.infrastructure.celery import app
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.job_applications.repository import (
    SqlApplicationRunRepository,
    SqlJobApplicationRepository,
)
from backend.app.maintenance.repository import (
    LocalVolumeStorageLister,
    SqlApplicationRunRecoveryProbe,
    SqlMaintenanceDocumentRepository,
)
from backend.app.maintenance.service import MaintenanceService

# Sweeping in bounded batches keeps the row locks taken by ``SKIP LOCKED`` short
# and lets a backlog be drained by several workers in parallel (§11.8).
_EXPIRE_BATCH_SIZE = 100
_REPUBLISH_BATCH_SIZE = 100
_CLEANUP_BATCH_SIZE = 500


async def _authorize_noop(actor: Actor, job_id: UUID) -> None:
    """The sweep runs internally; no per-actor authorization is needed."""
    return None


def _build_maintenance(
    resources: RuntimeResources, session: AsyncSession
) -> MaintenanceService:
    """Assemble the maintenance service over one transaction."""
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
    return MaintenanceService(
        approval_repo=approval_repo,
        arun_repo=arun_repo,
        app_repo=app_repo,
        run_service=run_service,
        approval_service=approval_service,
    )


async def _expire(
    resources: RuntimeResources, session: AsyncSession, before: datetime
) -> int:
    maintenance = _build_maintenance(resources, session)
    return await maintenance.expire_pending_approvals(before, batch_size=_EXPIRE_BATCH_SIZE)


async def _republish(resources: RuntimeResources, session: AsyncSession) -> dict[str, object]:
    settings = get_settings()
    maintenance = _build_maintenance(resources, session)
    # Documents are re-published through the retry task name. The parse service
    # guards on (parser_version, attempt), so re-publishing a document whose task
    # was already consumed is a no-op — and a *duplicate* publication is harmless
    # for the same reason.
    document_enqueuer = CeleryParseEnqueuer(app, "documents.retry_parse")

    def enqueue_document(document_id: UUID, attempt: int) -> None:
        document_enqueuer.enqueue_parse(
            document_id, attempt=attempt, parser_version=settings.parser_version
        )

    outcome = await maintenance.republish_queued(
        documents=SqlMaintenanceDocumentRepository(session),
        runs=SqlAgentRunRepository(session),
        enqueuer=CeleryRunEnqueuer(app),
        document_enqueue=enqueue_document,
        parser_version=settings.parser_version,
        queued_grace=timedelta(seconds=settings.republish_queued_after_seconds),
        run_lease=timedelta(seconds=settings.run_lease_seconds),
        recovery_probe=SqlApplicationRunRecoveryProbe(session),
        batch_size=_REPUBLISH_BATCH_SIZE,
    )
    return {
        "documents": outcome.documents,
        "match_runs": outcome.match_runs,
        "application_runs": outcome.application_runs,
        "total": outcome.total,
    }


async def _cleanup(resources: RuntimeResources, session: AsyncSession) -> dict[str, object]:
    settings = get_settings()
    maintenance = _build_maintenance(resources, session)
    outcome = await maintenance.cleanup_orphan_files(
        storage=LocalVolumeStorageLister(resources.storage_root),
        documents=SqlMaintenanceDocumentRepository(session),
        safety_window=timedelta(minutes=settings.orphan_file_safety_window_minutes),
        batch_size=_CLEANUP_BATCH_SIZE,
    )
    return {
        "scanned": outcome.scanned,
        "deleted": outcome.deleted,
        "skipped_referenced": outcome.skipped_referenced,
        "skipped_recent": outcome.skipped_recent,
        "skipped_malformed": outcome.skipped_malformed,
    }


@shared_task(name="maintenance.expire_approvals", bind=True)  # type: ignore[untyped-decorator]
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


@shared_task(name="maintenance.republish_queued", bind=True)  # type: ignore[untyped-decorator]
def republish_queued(self: Task) -> dict[str, object]:
    """Re-deliver work whose publication never landed (§14.4, FIN-006).

    The service publishes tasks while holding no write lock on business rows: it
    reads the backlogs and hands work to the broker. The volume of publications is
    bounded per cycle by the batch cap, and the next cycle picks up whatever is
    left, so a large outage drains steadily instead of stampeding the broker.
    """
    resources = RuntimeResources.build(get_settings())

    async def _run() -> dict[str, object]:
        session = resources.session_factory()
        try:
            result = await _republish(resources, session)
            await session.commit()
            return {"status": "ok", **result}
        finally:
            await session.close()
            await resources.close()

    return asyncio.run(_run())


@shared_task(name="maintenance.cleanup_orphan_files", bind=True)  # type: ignore[untyped-decorator]
def cleanup_orphan_files(self: Task) -> dict[str, object]:
    """Delete stored objects with no database reference (§14.3, FIN-006)."""
    resources = RuntimeResources.build(get_settings())

    async def _run() -> dict[str, object]:
        session = resources.session_factory()
        try:
            result = await _cleanup(resources, session)
            await session.commit()
            return {"status": "ok", **result}
        finally:
            await session.close()
            await resources.close()

    return asyncio.run(_run())
