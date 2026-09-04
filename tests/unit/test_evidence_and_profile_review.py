"""D09 acceptance: Profile confirmation + EvidenceChunk + source pinning.

Uses in-memory fakes for the repositories so the service-layer invariants
(optimistic lock, single READY per candidate, cross-document evidence
rejection, chunk-index uniqueness) are verified without a live Postgres.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.candidates.models import (
    CandidateProfile,
    CandidateProfileStatus,
    EvidenceChunk,
)
from backend.app.candidates.schemas import (
    CandidateProfileEdit,
    CandidateProfileResponse,
    EvidenceChunkCreate,
    EvidenceChunkResponse,
    EvidenceLocator,
)
from backend.app.candidates.service import ProfileReviewService
from backend.app.core.errors import AppError


def _hr() -> Actor:
    return Actor(user_id=uuid4(), username="hr", role=UserRole.HR)


def _non_hr() -> Actor:
    return Actor(user_id=uuid4(), username="viewer", role=UserRole.HIRING_MANAGER)


def _profile(
    *,
    candidate_id: UUID,
    document_id: UUID,
    status: CandidateProfileStatus = CandidateProfileStatus.REVIEW_REQUIRED,
    version: int = 1,
) -> CandidateProfile:
    now = datetime.now(UTC)
    return CandidateProfile(
        id=uuid4(),
        candidate_id=candidate_id,
        document_id=document_id,
        version_no=1,
        status=status,
        profile_json={"full_name": "测试候选人"},
        normalized_skills=[],
        years_experience=None,
        education_level=None,
        schema_version="v1",
        version=version,
        created_at=now,
        updated_at=now,
    )


@dataclass
class FakeCandidateProfileRepository:
    profiles: dict[UUID, CandidateProfile]

    async def save(self, profile: CandidateProfile) -> None:
        self.profiles[profile.id] = profile

    async def next_version_no(self, candidate_id: UUID) -> int:
        return 1

    async def get(self, profile_id: UUID) -> CandidateProfile | None:
        return self.profiles.get(profile_id)

    async def list_ready_versions(self, candidate_id: UUID) -> list[CandidateProfile]:
        return [
            p
            for p in self.profiles.values()
            if p.candidate_id == candidate_id and p.status is CandidateProfileStatus.READY
        ]


@dataclass
class FakeEvidenceChunkRepository:
    chunks: list[EvidenceChunk]

    async def save(self, chunk: EvidenceChunk) -> None:
        self.chunks.append(chunk)

    async def list_by_profile(self, profile_id: UUID) -> list[EvidenceChunk]:
        return [c for c in self.chunks if c.candidate_profile_id == profile_id]

    async def exists_index(self, document_id: UUID, chunk_index: int) -> bool:
        return any(
            c.document_id == document_id and c.chunk_index == chunk_index for c in self.chunks
        )


def _service(
    profile: CandidateProfile,
    *,
    extra_profiles: list[CandidateProfile] | None = None,
    chunks: list[EvidenceChunk] | None = None,
) -> ProfileReviewService:
    profiles = {profile.id: profile}
    for p in extra_profiles or []:
        profiles[p.id] = p
    return ProfileReviewService(
        profile_repo=FakeCandidateProfileRepository(profiles),
        chunk_repo=FakeEvidenceChunkRepository(chunks or []),
    )


def _edit() -> CandidateProfileEdit:
    return CandidateProfileEdit(
        profile_json={"full_name": "测试候选人", "skills": ["python"]},
        normalized_skills=["Python"],
        years_experience=3.0,
        education_level="BACHELOR",
    )


def _chunk_create(profile: CandidateProfile, *, document_id: UUID | None = None) -> EvidenceChunkCreate:  # noqa: E501
    return EvidenceChunkCreate(
        candidate_profile_id=profile.id,
        document_id=document_id or profile.document_id,
        chunk_index=0,
        section_type="skill",
        locator=EvidenceLocator(kind="pdf", page_number=1, block_index=0, char_start=0, char_end=10),  # noqa: E501
        text="熟悉 Python 与 FastAPI",
    )


def test_confirm_profile_moves_to_ready_and_attests() -> None:
    candidate_id = uuid4()
    document_id = uuid4()
    profile = _profile(candidate_id=candidate_id, document_id=document_id)
    service = _service(profile)

    async def run() -> CandidateProfileResponse:
        return await service.confirm_profile(
            actor=_hr(), profile_id=profile.id, edit=_edit(), expected_version=1
        )

    result = asyncio.run(run())
    assert result.status.value == "READY"
    assert result.confirmed_by is not None
    assert result.confirmed_at is not None
    assert result.version == 2


def test_optimistic_lock_conflict_returns_409() -> None:
    profile = _profile(candidate_id=uuid4(), document_id=uuid4(), version=3)
    service = _service(profile)

    async def run() -> None:
        await service.confirm_profile(
            actor=_hr(), profile_id=profile.id, edit=_edit(), expected_version=1
        )

    with pytest.raises(AppError) as exc:
        asyncio.run(run())
    assert exc.value.code == "PROFILE_VERSION_CONFLICT"
    assert exc.value.http_status == 409


def test_only_one_ready_supersedes_prior_version() -> None:
    candidate_id = uuid4()
    document_id = uuid4()
    prior = _profile(candidate_id=candidate_id, document_id=document_id, status=CandidateProfileStatus.READY, version=1)  # noqa: E501
    current = _profile(candidate_id=candidate_id, document_id=document_id, version=2)
    service = _service(current, extra_profiles=[prior])

    async def run() -> None:
        await service.confirm_profile(
            actor=_hr(), profile_id=current.id, edit=_edit(), expected_version=2
        )

    asyncio.run(run())
    assert prior.status.value == "SUPERSEDED"
    assert current.status.value == "READY"


def test_confirm_non_reviewable_is_rejected() -> None:
    profile = _profile(candidate_id=uuid4(), document_id=uuid4(), status=CandidateProfileStatus.READY)  # noqa: E501
    service = _service(profile)

    async def run() -> None:
        await service.confirm_profile(
            actor=_hr(), profile_id=profile.id, edit=_edit(), expected_version=1
        )

    with pytest.raises(AppError) as exc:
        asyncio.run(run())
    assert exc.value.code == "PROFILE_NOT_REVIEWABLE"
    assert exc.value.http_status == 409


def test_cross_document_evidence_is_rejected() -> None:
    profile = _profile(candidate_id=uuid4(), document_id=uuid4())
    other_doc = uuid4()
    service = _service(profile)

    async def run() -> None:
        await service.create_evidence_chunks(actor=_hr(), chunks=[_chunk_create(profile, document_id=other_doc)])  # noqa: E501

    with pytest.raises(AppError) as exc:
        asyncio.run(run())
    assert exc.value.code == "EVIDENCE_CROSS_DOCUMENT_REJECTED"
    assert exc.value.http_status == 422


def test_evidence_chunk_index_duplicate_is_rejected() -> None:
    profile = _profile(candidate_id=uuid4(), document_id=uuid4())
    first = _chunk_create(profile, document_id=profile.document_id)
    dup = _chunk_create(profile, document_id=profile.document_id)
    service = _service(profile)

    async def run() -> list[EvidenceChunkResponse]:
        return await service.create_evidence_chunks(actor=_hr(), chunks=[first, dup])

    with pytest.raises(AppError) as exc:
        asyncio.run(run())
    assert exc.value.code == "EVIDENCE_CHUNK_INDEX_DUPLICATE"
    assert exc.value.http_status == 409


def test_evidence_chunk_preserves_locator_and_sha256() -> None:
    profile = _profile(candidate_id=uuid4(), document_id=uuid4())
    service = _service(profile)

    async def run() -> list[EvidenceChunkResponse]:
        return await service.create_evidence_chunks(actor=_hr(), chunks=[_chunk_create(profile)])

    chunks = asyncio.run(run())
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.chunk_index == 0
    assert chunk.section_type == "skill"
    assert chunk.locator_json == {
        "kind": "pdf",
        "page_number": 1,
        "block_index": 0,
        "paragraph_index": None,
        "table_index": None,
        "row_index": None,
        "cell_index": None,
        "char_start": 0,
        "char_end": 10,
    }
    import hashlib

    assert chunk.text_sha256 == hashlib.sha256("熟悉 Python 与 FastAPI".encode()).hexdigest()


def test_list_evidence_returns_pinned_chunks() -> None:
    profile = _profile(candidate_id=uuid4(), document_id=uuid4())
    service = _service(profile)

    async def run() -> list[EvidenceChunkResponse]:
        await service.create_evidence_chunks(actor=_hr(), chunks=[_chunk_create(profile)])
        return await service.list_evidence(actor=_hr(), profile_id=profile.id)

    chunks = asyncio.run(run())
    assert len(chunks) == 1
    assert chunks[0].candidate_profile_id == profile.id


def test_non_hr_cannot_confirm_or_pin_evidence() -> None:
    profile = _profile(candidate_id=uuid4(), document_id=uuid4())
    service = _service(profile)

    async def run() -> None:
        await service.confirm_profile(
            actor=_non_hr(), profile_id=profile.id, edit=_edit(), expected_version=1
        )

    with pytest.raises(AppError) as exc:
        asyncio.run(run())
    assert exc.value.code == "FORBIDDEN"
    assert exc.value.http_status == 403


def test_missing_profile_returns_404() -> None:
    profile = _profile(candidate_id=uuid4(), document_id=uuid4())
    service = _service(profile)

    async def run() -> None:
        await service.create_evidence_chunks(
            actor=_hr(),
            chunks=[_chunk_create(profile, document_id=profile.document_id).model_copy(update={"candidate_profile_id": uuid4()})],  # noqa: E501
        )

    with pytest.raises(AppError) as exc:
        asyncio.run(run())
    assert exc.value.code == "PROFILE_NOT_FOUND"
    assert exc.value.http_status == 404
