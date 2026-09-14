"""Celery candidate tasks: thin wrappers that build context and call the service.

Two task families live here:

* ``candidates.extract_profile`` — §7.4. Consumes a parsed ``ResumeDocument`` and
  produces the ``REVIEW_REQUIRED`` ``CandidateProfile`` draft. Idempotent per
  document: a re-delivered task finds the existing draft and skips.
* ``embeddings.generate_chunks`` — fills ``EvidenceChunk.embedding`` after an HR
  reviewer confirms a profile.

Per detailed design §14 the task only parses parameters, builds the worker's
resources, and invokes the application service. Idempotency is enforced by the
database (existing draft / already-embedded chunk), so Celery at-least-once
redelivery is safe. Transient failures (DB/storage) use exponential backoff with
jitter; ``EmbeddingDimensionError`` is permanent and must not be retried.

Async note: each database/embedding workflow runs inside a *single* event loop
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
import random
from typing import Any
from uuid import UUID

from celery import Task, shared_task  # type: ignore[import-untyped]

from backend.app.auth.models import User
from backend.app.auth.tokens import Actor
from backend.app.candidates.embedding_service import EmbeddingService
from backend.app.candidates.repository import (
    SqlCandidateProfileRepository,
    SqlCandidateRepository,
    SqlEvidenceChunkRepository,
)
from backend.app.candidates.service import ProfileExtractionService
from backend.app.core.errors import AppError
from backend.app.core.settings import get_settings
from backend.app.documents.models import ResumeDocument
from backend.app.documents.parse_service import parsed_from_json
from backend.app.infrastructure.embedding import EmbeddingDimensionError, build_embedding_gateway
from backend.app.infrastructure.model_gateway import build_model_gateway
from backend.app.infrastructure.runtime import RuntimeResources


async def _run_extraction_async(
    resources: RuntimeResources, document_id: UUID
) -> dict[str, Any]:
    session = resources.session_factory()
    try:
        document = await session.get(ResumeDocument, document_id)
        if document is None:
            return {"status": "skipped", "reason": "document_not_found"}
        if not document.parsed_json:
            # A parse that never produced blocks has nothing to extract from.
            return {"status": "skipped", "reason": "document_not_parsed"}

        profiles = SqlCandidateProfileRepository(session)
        existing = await profiles.get_by_document_id(document_id)
        if existing is not None:
            # At-least-once delivery: one document yields exactly one draft.
            return {
                "status": "skipped",
                "reason": "already_extracted",
                "profile_id": str(existing.id),
            }

        uploader = await session.get(User, document.uploaded_by)
        if uploader is None:
            return {"status": "skipped", "reason": "uploader_not_found"}

        service = ProfileExtractionService(
            SqlCandidateRepository(session), profiles, build_model_gateway(get_settings())
        )
        try:
            profile = await service.extract(
                actor=Actor(
                    user_id=uploader.id, username=uploader.username, role=uploader.role
                ),
                document=document,
                parsed=parsed_from_json(document.parsed_json),
            )
        except AppError as error:
            if error.code == "FORBIDDEN":
                # Only HR uploads documents, so this is a data-consistency signal,
                # not a transient failure: never retry it.
                return {"status": "skipped", "reason": "uploader_not_hr"}
            raise
        await session.commit()
        return {"status": "ok", "profile_id": str(profile.id)}
    finally:
        await session.close()
        # Tear down the loop-bound engine/redis within the same event loop so no
        # loop-bound connection survives into the next task's loop.
        await resources.close()


def _run_extraction(document_id: str) -> dict[str, Any]:
    resources = RuntimeResources.build(get_settings())
    return asyncio.run(_run_extraction_async(resources, UUID(document_id)))


@shared_task(name="candidates.extract_profile", bind=True)  # type: ignore[untyped-decorator]
def extract_profile(self: Task, document_id: str) -> dict[str, Any]:
    try:
        return _run_extraction(document_id)
    except Exception as error:
        settings = get_settings()
        attempt_no = self.request.retries
        max_retries = settings.max_transient_retries
        if attempt_no >= max_retries:
            return {"status": "transient_exhausted", "document_id": document_id}
        countdown = min(2**attempt_no, 30) + random.uniform(0, 2)
        self.retry(exc=error, countdown=countdown, max_retries=max_retries)
        return {"status": "retrying"}  # task.retry raises; unreachable


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
