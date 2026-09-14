"""Celery document tasks: thin wrappers that build context and call the service.

Per detailed design §14 (Celery Task only parses parameters, builds the
request/task context, and invokes the application service). All lifecycle logic
lives in :mod:`backend.app.documents.parse_service`; this module wires it to a
worker process's database/storage resources and handles Celery retry/backoff.

Async note: the whole database/storage workflow runs inside a *single* event
loop (a single ``asyncio.run``). The async SQLAlchemy engine and the redis
client are both loop-bound, and Celery's prefork worker forks child processes
after module import — so we must not share a cached ``RuntimeResources`` across
tasks, nor split the work across several ``asyncio.run`` calls (which would
attach the connection to different loops and raise "attached to a different
loop" / "cannot use Connection.transaction() in a manually started
transaction"). We therefore build resources per task invocation and dispose them
in the same loop.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from typing import Any
from uuid import UUID

from celery import Task, shared_task  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.settings import get_settings
from backend.app.documents.models import DocumentStatus
from backend.app.documents.parse_service import (
    CeleryParseEnqueuer,
    DocumentParseService,
    TransientParseError,
    build_parser_registry,
)
from backend.app.documents.repository import SqlAlchemyDocumentRepository
from backend.app.infrastructure.celery import app
from backend.app.infrastructure.runtime import RuntimeResources


def _build_service(
    resources: RuntimeResources, repository: SqlAlchemyDocumentRepository
) -> DocumentParseService:
    settings = get_settings()
    return DocumentParseService(
        documents=repository,
        storage=resources.storage,
        parsers=build_parser_registry(parser_version=settings.parser_version),
        enqueue=CeleryParseEnqueuer(app, "documents.retry_parse"),
        settings=settings,
    )


class _NeedsRetry(Exception):
    """Internal signal: a transient failure should be retried by Celery."""

    def __init__(self, error: TransientParseError, countdown: float, max_retries: int) -> None:
        self.error = error
        self.countdown = countdown
        self.max_retries = max_retries


async def _mark_permanent_failure(
    session_factory: Callable[[], AsyncSession], document_id: UUID, code: str, message: str
) -> None:
    repository = SqlAlchemyDocumentRepository(session_factory())
    document = await repository.get_for_update(document_id)
    if document is None:
        return
    if document.status in (DocumentStatus.REVIEW_REQUIRED, DocumentStatus.READY):
        return
    document.status = DocumentStatus.FAILED
    document.error_code = code
    document.error_message_safe = message
    document.retryable = False
    await repository.save(document)
    await repository.commit()
    await repository.close()


async def _run_parse_async(
    resources: RuntimeResources,
    task: Task,
    document_id: UUID,
    *,
    attempt: int,
    parser_version: str,
) -> str | None:
    settings = get_settings()
    repository = SqlAlchemyDocumentRepository(resources.session_factory())
    service = _build_service(resources, repository)
    try:
        status = await service.process_parse(
            document_id, attempt=attempt, parser_version=parser_version
        )
        return status.value if status is not None else None
    except TransientParseError as error:
        attempt_no = task.request.retries
        max_retries = settings.max_transient_retries
        if attempt_no >= max_retries:
            await _mark_permanent_failure(
                resources.session_factory,
                document_id,
                "TRANSIENT_RETRIES_EXHAUSTED",
                error.safe_message,
            )
            return DocumentStatus.FAILED.value
        countdown = min(2**attempt_no, 30) + random.uniform(0, 2)
        raise _NeedsRetry(error, countdown, max_retries)
    finally:
        await repository.close()
        # Tear down the loop-bound engine/redis within the same event loop so no
        # loop-bound connection survives into the next task's loop.
        await resources.close()


def _run_parse(
    task: Task, document_id: str, *, attempt: int, parser_version: str
) -> str | None:
    resources = RuntimeResources.build(get_settings())
    document_id_uuid = UUID(document_id)
    try:
        return asyncio.run(
            _run_parse_async(
                resources,
                task,
                document_id_uuid,
                attempt=attempt,
                parser_version=parser_version,
            )
        )
    except _NeedsRetry as signal:
        task.retry(exc=signal.error, countdown=signal.countdown, max_retries=signal.max_retries)
        return None  # task.retry raises; unreachable


@shared_task(name="documents.parse", bind=True)  # type: ignore[untyped-decorator]
def parse_document(
    self: Task, document_id: str, *, attempt: int, parser_version: str
) -> str | None:
    return _run_parse(self, document_id, attempt=attempt, parser_version=parser_version)


@shared_task(name="documents.retry_parse", bind=True)  # type: ignore[untyped-decorator]
def retry_parse_document(
    self: Task, document_id: str, *, attempt: int, parser_version: str
) -> str | None:
    return _run_parse(self, document_id, attempt=attempt, parser_version=parser_version)
