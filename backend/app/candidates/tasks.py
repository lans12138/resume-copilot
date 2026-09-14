"""Celery embedding tasks: thin wrappers that build context and call the service.

Per detailed design §14, the task only parses parameters, builds the worker's
resources, and invokes :class:`EmbeddingService`. Idempotency is enforced by the
database (a chunk already embedded with the same model+version is skipped), so
Celery at-least-once redelivery is safe. Transient failures (DB/storage) use
exponential backoff with jitter; ``EmbeddingDimensionError`` is permanent and
must not be retried.

Async note: the whole database/embedding workflow runs inside a *single* event
loop (a single ``asyncio.run``). The async SQLAlchemy engine and the redis client
are both loop-bound, and Celery's prefork worker forks child processes after
module import — so we must not share a cached ``RuntimeResources`` across tasks,
nor split the work across several ``asyncio.run`` calls (which would attach the
connection to different loops and raise "attached to a different loop" /
"cannot use Connection.transaction() in a manually started transaction"). We
therefore build resources per task invocation and dispose them in the same loop.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any
from uuid import UUID

from celery import Task, shared_task  # type: ignore[import-untyped]

from backend.app.candidates.embedding_service import EmbeddingService
from backend.app.candidates.repository import SqlEvidenceChunkRepository
from backend.app.core.settings import get_settings
from backend.app.infrastructure.embedding import EmbeddingDimensionError, build_embedding_gateway
from backend.app.infrastructure.runtime import RuntimeResources


def _build_service(
    resources: RuntimeResources, repo: SqlEvidenceChunkRepository
) -> EmbeddingService:
    settings = get_settings()
    gateway = build_embedding_gateway(settings)
    return EmbeddingService(
        chunk_repo=repo,
        gateway=gateway,
        model=settings.embedding_model,
        version=gateway.version,
        expected_dimension=int(settings.embedding_dimension),
        batch_size=settings.embedding_batch_size,
    )


async def _run_embeddings_async(
    resources: RuntimeResources, profile_id: str
) -> dict[str, Any]:
    session = resources.session_factory()
    repo = SqlEvidenceChunkRepository(session)
    service = _build_service(resources, repo)
    try:
        result = await service.generate_for_profile(UUID(profile_id))
        await repo.commit()
        return {
            "status": "ok",
            "profile_id": profile_id,
            "generated": result.generated,
            "skipped": result.skipped,
        }
    except EmbeddingDimensionError as error:
        return {
            "status": "permanent_failure",
            "reason": "embedding_dimension_mismatch",
            "model": error.model,
            "got": error.got,
            "expected": error.expected,
        }
    finally:
        await repo.close()
        # Tear down the loop-bound engine/redis within the same event loop so no
        # loop-bound connection survives into the next task's loop.
        await resources.close()


def _run_embeddings(profile_id: str) -> dict[str, Any]:
    resources = RuntimeResources.build(get_settings())
    return asyncio.run(_run_embeddings_async(resources, profile_id))


@shared_task(name="embeddings.generate_chunks", bind=True)  # type: ignore[untyped-decorator]
def generate_chunk_embeddings(self: Task, profile_id: str) -> dict[str, Any]:
    try:
        return _run_embeddings(profile_id)
    except Exception as error:
        settings = get_settings()
        attempt_no = self.request.retries
        max_retries = settings.max_transient_retries
        if attempt_no >= max_retries:
            return {"status": "transient_exhausted", "profile_id": profile_id}
        countdown = min(2**attempt_no, 30) + random.uniform(0, 2)
        self.retry(exc=error, countdown=countdown, max_retries=max_retries)
        return {"status": "retrying"}  # task.retry raises; unreachable
