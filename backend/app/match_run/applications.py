"""Resolve ranked profiles to durable JobApplication aggregates.

A MatchRun snapshot is the hand-off point from batch matching to the per-person
ApplicationRun workflow.  Every ranked profile therefore needs a real
``JobApplication`` id, not a profile-id placeholder.  The PostgreSQL upsert is
idempotent on ``(job_id, candidate_id)`` and deliberately leaves the current
application status unchanged when an application already exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.candidates.models import CandidateProfile
from backend.app.job_applications.models import ApplicationStatus, JobApplication


class SqlApplicationsProvider:
    """Create or reuse the applications required by one ranking snapshot."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(
        self, *, job_id: UUID, profile_ids: Sequence[UUID]
    ) -> dict[UUID, UUID]:
        if not profile_ids:
            return {}

        unique_profile_ids = list(dict.fromkeys(profile_ids))
        result = await self._session.execute(
            select(CandidateProfile.id, CandidateProfile.candidate_id).where(
                CandidateProfile.id.in_(unique_profile_ids)
            )
        )
        candidate_by_profile: dict[UUID, UUID] = {
            profile_id: candidate_id
            for profile_id, candidate_id in result.tuples().all()
        }
        missing = set(unique_profile_ids) - candidate_by_profile.keys()
        if missing:
            raise RuntimeError(
                "ranking snapshot references missing candidate profiles: "
                + ", ".join(sorted(str(profile_id) for profile_id in missing))
            )

        candidate_ids = list(dict.fromkeys(candidate_by_profile.values()))
        await self._session.execute(
            insert(JobApplication)
            .values(
                [
                    {
                        "id": uuid4(),
                        "job_id": job_id,
                        "candidate_id": candidate_id,
                        "status": ApplicationStatus.CREATED.value,
                        "version": 1,
                    }
                    for candidate_id in candidate_ids
                ]
            )
            .on_conflict_do_nothing(
                constraint="uq_job_applications_job_candidate"
            )
        )

        applications = await self._session.execute(
            select(JobApplication.id, JobApplication.candidate_id).where(
                JobApplication.job_id == job_id,
                JobApplication.candidate_id.in_(candidate_ids),
            )
        )
        application_by_candidate = {
            candidate_id: application_id
            for application_id, candidate_id in applications.all()
        }
        return {
            profile_id: application_by_candidate[candidate_id]
            for profile_id, candidate_id in candidate_by_profile.items()
        }
