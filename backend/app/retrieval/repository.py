"""Retrieval data ports (IMP-014).

The repository is the only boundary that touches Postgres. It projects READY
profiles and their chunk embeddings into the plain dataclasses the recallers
consume, so the recall logic stays pure and testable without a database. The
vector channel is exact (chunks are fetched and cosine'd in-process, no HNSW),
matching the MVP "precise vector retrieval" constraint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.candidates.models import (
    CandidateProfile,
    CandidateProfileStatus,
    EvidenceChunk,
)
from backend.app.jobs.models import JobVersion
from backend.app.jobs.schemas import JobRequirements
from backend.app.retrieval.models import ChunkVector, JobQuery, ReadyProfile


class RetrievalRepository(Protocol):
    async def list_ready_profiles(self) -> list[ReadyProfile]: ...

    async def list_chunk_vectors(self, profile_ids: list[UUID]) -> list[ChunkVector]: ...

    async def get_job_query(self, job_version_id: UUID) -> JobQuery: ...


class SqlRetrievalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_ready_profiles(self) -> list[ReadyProfile]:
        rows = await self.session.scalars(
            select(CandidateProfile).where(
                CandidateProfile.status == CandidateProfileStatus.READY
            )
        )
        return [self._project_profile(profile) for profile in rows]

    async def list_chunk_vectors(self, profile_ids: list[UUID]) -> list[ChunkVector]:
        if not profile_ids:
            return []
        result = await self.session.execute(
            select(EvidenceChunk.candidate_profile_id, EvidenceChunk.embedding).where(
                EvidenceChunk.candidate_profile_id.in_(profile_ids),
                EvidenceChunk.embedding.isnot(None),
            )
        )
        return [
            ChunkVector(candidate_profile_id=profile_id, embedding=list(embedding))
            for profile_id, embedding in result.all()
            if embedding is not None
        ]

    async def get_job_query(self, job_version_id: UUID) -> JobQuery:
        job_version = await self.session.get(JobVersion, job_version_id)
        if job_version is None:
            raise ValueError(f"job version not found: {job_version_id}")
        requirements = JobRequirements.model_validate(job_version.requirements_json)
        return JobQuery(
            job_version_id=job_version.id,
            required_skills=list(requirements.required_skills),
            preferred_skills=list(requirements.preferred_skills),
            min_years=requirements.minimum_years_experience,
            required_education=requirements.education_level,
            description_text=job_version.description_text,
            requirements_json=dict(job_version.requirements_json),
        )

    @staticmethod
    def _project_profile(profile: CandidateProfile) -> ReadyProfile:
        return ReadyProfile(
            profile_id=profile.id,
            normalized_skills=list(profile.normalized_skills),
            years_experience=float(profile.years_experience)
            if profile.years_experience is not None
            else None,
            education_level=profile.education_level,
            profile_json=dict(profile.profile_json),
        )


@dataclass
class InMemoryRetrievalRepository:
    """Test double holding pre-built projections."""

    profiles: list[ReadyProfile] = field(default_factory=list)
    chunk_vectors: list[ChunkVector] = field(default_factory=list)
    job_queries: dict[UUID, JobQuery] = field(default_factory=dict)

    async def list_ready_profiles(self) -> list[ReadyProfile]:
        return list(self.profiles)

    async def list_chunk_vectors(self, profile_ids: list[UUID]) -> list[ChunkVector]:
        wanted = set(profile_ids)
        return [chunk for chunk in self.chunk_vectors if chunk.candidate_profile_id in wanted]

    async def get_job_query(self, job_version_id: UUID) -> JobQuery:
        query = self.job_queries.get(job_version_id)
        if query is None:
            raise ValueError(f"job version not found: {job_version_id}")
        return query
