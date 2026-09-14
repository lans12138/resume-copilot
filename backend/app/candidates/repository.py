"""Candidate persistence ports and SQLAlchemy adapters.

Repositories own session-local writes only; they never commit. The surrounding
unit-of-work (route or worker) decides when to flush, so a failed extraction
rolls back cleanly and duplicate deliveries do not create orphan rows.
"""

from __future__ import annotations

from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.candidates.models import (
    Candidate,
    CandidateProfile,
    CandidateProfileStatus,
    EvidenceChunk,
)
from backend.app.retrieval.models import ReadyProfileView


class CandidateRepository(Protocol):
    async def save(self, candidate: Candidate) -> None: ...

    async def get_by_email_hash(self, email_hash: str | None) -> Candidate | None: ...


class CandidateProfileRepository(Protocol):
    async def save(self, profile: CandidateProfile) -> None: ...

    async def flush_and_refresh(self, profile: CandidateProfile) -> None: ...

    async def next_version_no(self, candidate_id: UUID) -> int: ...

    async def get(self, profile_id: UUID) -> CandidateProfile | None: ...

    async def get_by_document_id(self, document_id: UUID) -> CandidateProfile | None: ...

    async def list_ready_versions(self, candidate_id: UUID) -> list[CandidateProfile]: ...


class EvidenceChunkRepository(Protocol):
    async def save(self, chunk: EvidenceChunk) -> None: ...

    async def list_by_profile(self, profile_id: UUID) -> list[EvidenceChunk]: ...

    async def exists_index(self, document_id: UUID, chunk_index: int) -> bool: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def close(self) -> None: ...


class SqlCandidateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(self, candidate: Candidate) -> None:
        self.session.add(candidate)

    async def get_by_email_hash(self, email_hash: str | None) -> Candidate | None:
        if email_hash is None:
            return None
        return cast(
            Candidate | None,
            await self.session.scalar(
                select(Candidate).where(Candidate.normalized_email_hash == email_hash)
            ),
        )


class SqlCandidateProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(self, profile: CandidateProfile) -> None:
        self.session.add(profile)

    async def flush_and_refresh(self, profile: CandidateProfile) -> None:
        """Emit the pending UPDATE and re-read the server-owned columns.

        ``updated_at`` carries ``onupdate=func.now()``, so SQLAlchemy cannot derive
        the stored value from the statement it generates: after the flush the
        attribute is expired and the next read would trigger a lazy refresh. In an
        async session that lazy refresh has no greenlet to await on and raises
        ``MissingGreenlet`` instead of returning a value — which is what turned a
        successful confirm into a bare 500. Refreshing here, while the caller is
        still inside an await, keeps the instance usable for the response model.
        """
        await self.session.flush()
        await self.session.refresh(profile)

    async def next_version_no(self, candidate_id: UUID) -> int:
        current = await self.session.scalar(
            select(func.coalesce(func.max(CandidateProfile.version_no), 0)).where(
                CandidateProfile.candidate_id == candidate_id
            )
        )
        return int(current or 0) + 1

    async def get(self, profile_id: UUID) -> CandidateProfile | None:
        return await self.session.get(CandidateProfile, profile_id)

    async def get_by_document_id(self, document_id: UUID) -> CandidateProfile | None:
        """Newest profile draft extracted from a document (extraction idempotency)."""
        profile: CandidateProfile | None = await self.session.scalar(
            select(CandidateProfile)
            .where(CandidateProfile.document_id == document_id)
            .order_by(CandidateProfile.version_no.desc())
            .limit(1)
        )
        return profile

    async def list_ready_versions(self, candidate_id: UUID) -> list[CandidateProfile]:
        return list(
            await self.session.scalars(
                select(CandidateProfile).where(
                    CandidateProfile.candidate_id == candidate_id,
                    CandidateProfile.status == CandidateProfileStatus.READY,
                )
            )
        )

    async def list_ready_views(self) -> list[ReadyProfileView]:
        """READY profile projections joined with the candidate display name."""
        result = await self.session.execute(
            select(CandidateProfile, Candidate.display_name)
            .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
            .where(CandidateProfile.status == CandidateProfileStatus.READY)
        )
        views: list[ReadyProfileView] = []
        for profile, display_name in result.all():
            views.append(
                ReadyProfileView(
                    profile_id=profile.id,
                    display_name=display_name,
                    normalized_skills=list(profile.normalized_skills),
                    years_experience=float(profile.years_experience)
                    if profile.years_experience is not None
                    else None,
                    education_level=profile.education_level,
                )
            )
        return views


class SqlEvidenceChunkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(self, chunk: EvidenceChunk) -> None:
        self.session.add(chunk)

    async def list_by_profile(self, profile_id: UUID) -> list[EvidenceChunk]:
        return list(
            await self.session.scalars(
                select(EvidenceChunk)
                .where(EvidenceChunk.candidate_profile_id == profile_id)
                .order_by(EvidenceChunk.chunk_index)
            )
        )

    async def list_chunks(self, candidate_profile_id: UUID) -> list[EvidenceChunk]:
        """Expose the evidence-provider port used during report generation."""
        return await self.list_by_profile(candidate_profile_id)

    async def exists_index(self, document_id: UUID, chunk_index: int) -> bool:
        count = await self.session.scalar(
            select(func.count(EvidenceChunk.id)).where(
                EvidenceChunk.document_id == document_id,
                EvidenceChunk.chunk_index == chunk_index,
            )
        )
        return bool(count)

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()

    async def close(self) -> None:
        await self.session.close()
