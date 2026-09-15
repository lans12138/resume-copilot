"""Embedding generation and persistence for evidence chunks (IMP-013).

Batch the verbatim chunk texts through ``EmbeddingGateway``, validate every
returned vector's dimension, and persist ``embedding`` + ``embedding_model`` +
``embedding_version`` onto the chunk. Idempotency follows detailed design
§14.1's spirit for the ``embeddings.generate_chunks`` task: a chunk already
holding an embedding for the same model+version is skipped. The chunk's
``text_sha256`` is immutable, so a model+version match is sufficient to prove
the stored vector reflects the current text — Celery at-least-once redelivery
is therefore safe without extra bookkeeping.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from celery import Celery  # type: ignore[import-untyped]

from backend.app.candidates.models import EvidenceChunk
from backend.app.candidates.repository import EvidenceChunkRepository
from backend.app.infrastructure.embedding import EmbeddingDimensionError, EmbeddingGateway


@dataclass(frozen=True)
class EmbeddingResult:
    """Outcome of one embedding pass over a profile's chunks."""

    total: int
    embedded: int
    generated: int
    skipped: int


class EmbeddingEnqueuer(Protocol):
    """Delivers an embedding task. Implemented by Celery in production, in-memory in tests."""

    def enqueue(self, *, profile_id: UUID) -> None: ...


class CeleryEmbeddingEnqueuer:
    """Production ``EmbeddingEnqueuer`` that delivers the named Celery task."""

    def __init__(self, celery_app: Celery, task_name: str) -> None:
        self._celery_app = celery_app
        self._task_name = task_name

    def enqueue(self, *, profile_id: UUID) -> None:
        self._celery_app.send_task(self._task_name, args=[str(profile_id)])


class EmbeddingService:
    """Turns a profile's un-embedded chunks into persisted vectors."""

    def __init__(
        self,
        chunk_repo: EvidenceChunkRepository,
        gateway: EmbeddingGateway,
        *,
        model: str,
        version: str,
        expected_dimension: int,
        batch_size: int,
    ) -> None:
        self._chunks = chunk_repo
        self._gateway = gateway
        self._model = model
        self._version = version
        self._expected_dimension = expected_dimension
        self._batch_size = batch_size

    def _is_current(self, chunk: EvidenceChunk) -> bool:
        # Already embedded with the same model+version => reflects current text.
        return (
            chunk.embedding is not None
            and chunk.embedding_model == self._model
            and chunk.embedding_version == self._version
        )

    async def generate_for_profile(self, profile_id: UUID) -> EmbeddingResult:
        chunks = await self._chunks.list_by_profile(profile_id)
        return await self._embed(chunks)

    async def _embed(self, chunks: Sequence[EvidenceChunk]) -> EmbeddingResult:
        pending = [chunk for chunk in chunks if not self._is_current(chunk)]
        skipped = len(chunks) - len(pending)
        for start in range(0, len(pending), self._batch_size):
            batch = pending[start : start + self._batch_size]
            vectors = await self._gateway.embed([chunk.text for chunk in batch])
            if len(vectors) != len(batch):
                raise EmbeddingDimensionError(
                    model=self._model, got=len(vectors), expected=len(batch)
                )
            for chunk, vector in zip(batch, vectors, strict=True):
                if len(vector) != self._expected_dimension:
                    raise EmbeddingDimensionError(
                        model=self._model, got=len(vector), expected=self._expected_dimension
                    )
                chunk.embedding = vector
                chunk.embedding_model = self._model
                chunk.embedding_version = self._version
                await self._chunks.save(chunk)
        return EmbeddingResult(
            total=len(chunks),
            embedded=skipped + len(pending),
            generated=len(pending),
            skipped=skipped,
        )


class InMemoryEvidenceChunkRepository:
    """Test double for ``EvidenceChunkRepository``; holds chunks in a dict."""

    def __init__(self) -> None:
        self._chunks: dict[UUID, EvidenceChunk] = {}

    async def save(self, chunk: EvidenceChunk) -> None:
        self._chunks[chunk.id] = chunk

    async def list_by_profile(self, profile_id: UUID) -> list[EvidenceChunk]:
        return sorted(
            (c for c in self._chunks.values() if c.candidate_profile_id == profile_id),
            key=lambda c: c.chunk_index,
        )

    async def exists_index(self, document_id: UUID, chunk_index: int) -> bool:
        return any(
            c.document_id == document_id and c.chunk_index == chunk_index
            for c in self._chunks.values()
        )

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None
