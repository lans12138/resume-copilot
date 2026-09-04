"""IMP-013 tests: EmbeddingGateway, batching, dimension validation, idempotency.

Covers D11: Fake Embedding, dimension error, and repeat-generation (content-hash
idempotency). Runs with an in-memory chunk repository, no Postgres/Redis/Celery.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.candidates.embedding_service import (
    CeleryEmbeddingEnqueuer,
    EmbeddingEnqueuer,
    EmbeddingResult,
    EmbeddingService,
    InMemoryEvidenceChunkRepository,
)
from backend.app.candidates.models import (
    CandidateProfile,
    CandidateProfileStatus,
    EvidenceChunk,
)
from backend.app.candidates.schemas import CandidateProfileEdit
from backend.app.candidates.service import ProfileReviewService
from backend.app.infrastructure.embedding import EmbeddingDimensionError, FakeEmbeddingGateway


def _chunk(
    profile_id: UUID,
    document_id: UUID,
    index: int,
    text: str,
    *,
    embedding: list[float] | None = None,
    model: str | None = None,
    version: str | None = None,
) -> EvidenceChunk:
    return EvidenceChunk(
        id=uuid4(),
        document_id=document_id,
        candidate_profile_id=profile_id,
        chunk_index=index,
        section_type="experience",
        locator_json={"kind": "pdf", "page_number": 1},
        text=text,
        text_sha256=f"sha{index}",
        embedding=embedding,
        embedding_model=model,
        embedding_version=version,
        created_at=datetime.now(UTC),
    )


class _WrongDimGateway:
    """Returns vectors of a deliberately wrong dimension."""

    version = "wrong-v1"

    def __init__(self, dimension: int) -> None:
        self._dimension = dimension

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * self._dimension for _ in texts]


def _make_service(
    repo: InMemoryEvidenceChunkRepository,
    *,
    model: str = "qwen3.7-text-embedding",
    version: str = "fake-embed-v1",
    expected: int = 1024,
    batch: int = 4,
) -> EmbeddingService:
    return EmbeddingService(
        chunk_repo=repo,
        gateway=FakeEmbeddingGateway(dimension=1024),
        model=model,
        version=version,
        expected_dimension=expected,
        batch_size=batch,
    )


def _hr_actor() -> Actor:
    return Actor(user_id=uuid4(), username="hr", role=UserRole.HR)


def test_fake_embedding_is_deterministic_and_1024_dim() -> None:
    gateway = FakeEmbeddingGateway(dimension=1024)
    vectors = asyncio.run(gateway.embed(["hello", "world"]))
    assert len(vectors) == 2
    assert all(len(v) == 1024 for v in vectors)
    # Same input -> identical vector (stable for reproducible retrieval tests).
    again = asyncio.run(gateway.embed(["hello"]))
    assert again[0] == vectors[0]


def test_generate_persists_embedding_and_model_version() -> None:
    profile_id = uuid4()
    document_id = uuid4()
    repo: InMemoryEvidenceChunkRepository = InMemoryEvidenceChunkRepository()
    chunk = _chunk(profile_id, document_id, 0, "python, fastapi")
    asyncio.run(repo.save(chunk))

    result = asyncio.run(_make_service(repo).generate_for_profile(profile_id))

    assert chunk.embedding is not None
    assert len(chunk.embedding) == 1024
    assert chunk.embedding_model == "qwen3.7-text-embedding"
    assert chunk.embedding_version == "fake-embed-v1"
    assert result == EmbeddingResult(total=1, embedded=1, generated=1, skipped=0)


def test_dimension_mismatch_raises_and_leaves_chunk_unembedded() -> None:
    profile_id = uuid4()
    document_id = uuid4()
    repo = InMemoryEvidenceChunkRepository()
    chunk = _chunk(profile_id, document_id, 0, "python")
    asyncio.run(repo.save(chunk))

    service = EmbeddingService(
        chunk_repo=repo,
        gateway=_WrongDimGateway(512),
        model="m",
        version="v",
        expected_dimension=1024,
        batch_size=4,
    )
    raised = False
    try:
        asyncio.run(service.generate_for_profile(profile_id))
    except EmbeddingDimensionError as error:
        raised = True
        assert error.got == 512
        assert error.expected == 1024
    assert raised
    assert chunk.embedding is None


def test_duplicate_generation_is_idempotent() -> None:
    profile_id = uuid4()
    document_id = uuid4()
    repo = InMemoryEvidenceChunkRepository()
    chunk = _chunk(profile_id, document_id, 0, "python, fastapi")
    asyncio.run(repo.save(chunk))

    first = asyncio.run(_make_service(repo).generate_for_profile(profile_id))
    assert chunk.embedding is not None
    stored = list(chunk.embedding)
    second = asyncio.run(_make_service(repo).generate_for_profile(profile_id))

    assert first.generated == 1 and first.skipped == 0
    assert second.generated == 0 and second.skipped == 1
    assert list(chunk.embedding) == stored  # unchanged on repeat


def test_batch_processing_spans_multiple_batches() -> None:
    profile_id = uuid4()
    document_id = uuid4()
    repo = InMemoryEvidenceChunkRepository()
    chunks = [_chunk(profile_id, document_id, i, f"text-{i}") for i in range(10)]
    for chunk in chunks:
        asyncio.run(repo.save(chunk))

    result = asyncio.run(_make_service(repo, batch=4).generate_for_profile(profile_id))

    assert result.generated == 10
    assert all(c.embedding is not None and len(c.embedding) == 1024 for c in chunks)


def test_model_version_change_regenerates_embedding() -> None:
    profile_id = uuid4()
    document_id = uuid4()
    repo = InMemoryEvidenceChunkRepository()
    chunk = _chunk(profile_id, document_id, 0, "python")
    asyncio.run(repo.save(chunk))

    asyncio.run(_make_service(repo, version="v1").generate_for_profile(profile_id))
    assert chunk.embedding_version == "v1"
    # Re-run with a new version: model+version no longer match -> regenerate.
    result = asyncio.run(_make_service(repo, version="v2").generate_for_profile(profile_id))
    assert result.generated == 1 and result.skipped == 0
    assert chunk.embedding_version == "v2"


class _StubEnqueuer:
    """Records the profile_id passed to enqueue (structural EmbeddingEnqueuer)."""

    def __init__(self) -> None:
        self.enqueued: list[UUID] = []

    def enqueue(self, *, profile_id: UUID) -> None:
        self.enqueued.append(profile_id)


class _FakeProfileRepository:
    """Minimal CandidateProfileRepository for the confirm_profile trigger test."""

    def __init__(self, profile: CandidateProfile) -> None:
        self._profile = profile

    async def save(self, profile: CandidateProfile) -> None:
        self._profile = profile

    async def next_version_no(self, candidate_id: UUID) -> int:
        return 1

    async def get(self, profile_id: UUID) -> CandidateProfile | None:
        return self._profile if self._profile.id == profile_id else None

    async def list_ready_versions(self, candidate_id: UUID) -> list[CandidateProfile]:
        if self._profile.status == CandidateProfileStatus.READY:
            return [self._profile]
        return []


def test_confirm_profile_enqueues_embedding_task() -> None:
    profile_id = uuid4()
    document_id = uuid4()
    candidate_id = uuid4()
    profile = CandidateProfile(
        id=profile_id,
        candidate_id=candidate_id,
        document_id=document_id,
        version_no=1,
        version=1,
        status=CandidateProfileStatus.REVIEW_REQUIRED,
        profile_json={},
        normalized_skills=[],
        schema_version="v1",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    profiles = _FakeProfileRepository(profile)
    enqueuer = _StubEnqueuer()
    service = ProfileReviewService(profiles, InMemoryEvidenceChunkRepository())

    edit = CandidateProfileEdit(profile_json={}, normalized_skills=[])
    asyncio.run(
        service.confirm_profile(
            actor=_hr_actor(),
            profile_id=profile_id,
            edit=edit,
            expected_version=1,
            enqueue=enqueuer,
        )
    )

    assert enqueuer.enqueued == [profile_id]


class _StubCelery:
    """Records send_task calls without a real broker."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any]]] = []

    def send_task(self, name: str, args: list[Any]) -> None:
        self.calls.append((name, args))


def test_celery_enqueuer_payload() -> None:
    profile_id = uuid4()
    stub = _StubCelery()
    enqueuer: EmbeddingEnqueuer = CeleryEmbeddingEnqueuer(stub, "embeddings.generate_chunks")
    enqueuer.enqueue(profile_id=profile_id)

    assert stub.calls == [("embeddings.generate_chunks", [str(profile_id)])]
