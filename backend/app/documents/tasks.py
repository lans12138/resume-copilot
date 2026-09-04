"""Celery document tasks: thin wrappers that build context and call the service.

Per detailed design §14 (Celery Task only parses parameters, builds the
request/task context, and invokes the application service). All lifecycle logic
lives in :mod:`backend.app.documents.parse_service`; this module wires it to a
worker process's database/storage resources and handles Celery retry/backoff.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from uuid import UUID

from celery import Task  # type: ignore[import-untyped]
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

_cached_resources: RuntimeResources | None = None


def _resources() -> RuntimeResources:
    global _cached_resources
    if _cached_resources is None:
        _cached_resources = RuntimeResources.build(get_settings())
    return _cached_resources


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


def _run_parse(task: Task, document_id: str, *, attempt: int, parser_version: str) -> str | None:
    settings = get_settings()
    resources = _resources()
    repository = SqlAlchemyDocumentRepository(resources.session_factory())
    service = _build_service(resources, repository)
    try:
        status = asyncio.run(
            service.process_parse(UUID(document_id), attempt=attempt, parser_version=parser_version)
        )
    except TransientParseError as error:
        max_retries = settings.max_transient_retries
        attempt_no = task.request.retries
        if attempt_no >= max_retries:
            asyncio.run(
                _mark_permanent_failure(
                    resources.session_factory,
                    UUID(document_id),
                    "TRANSIENT_RETRIES_EXHAUSTED",
                    error.safe_message,
                )
            )
            return DocumentStatus.FAILED.value
        countdown = min(2**attempt_no, 30) + random.uniform(0, 2)
        task.retry(exc=error, countdown=countdown, max_retries=max_retries)
        return None  # task.retry raises; unreachable
    finally:
        asyncio.run(repository.close())
    return status.value if status is not None else None


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


@app.task(name="documents.parse", bind=True)  # type: ignore[untyped-decorator]
def parse_document(
    self: Task, document_id: str, *, attempt: int, parser_version: str
) -> str | None:
    return _run_parse(self, document_id, attempt=attempt, parser_version=parser_version)


@app.task(name="documents.retry_parse", bind=True)  # type: ignore[untyped-decorator]
def retry_parse_document(
    self: Task, document_id: str, *, attempt: int, parser_version: str
) -> str | None:
    return _run_parse(self, document_id, attempt=attempt, parser_version=parser_version)
