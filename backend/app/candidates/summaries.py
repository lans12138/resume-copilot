"""Display projections that let a page name a candidate instead of printing an id.

The ranking table, the report heading and the evidence deep link all answer the
same question — *who is this?* — and all of them used to answer it with a
truncated profile UUID. The presenter had to hold eight hex characters in their
head to connect a ranking row to its report and its approval (PORT-005).

Two decisions are worth stating, because both are easy to get subtly wrong:

* **Look up by id, never filter on READY.** A MatchRun freezes profile ids at run
  time. A profile superseded since then is still the person whose ranking was
  recorded, so filtering on ``status = READY`` here would blank the name of every
  candidate who re-uploaded a resume after the run — silently, and only for the
  rows the operator most needs to explain.
* **The summary is a projection, not a join the caller writes.** ``document_id``
  travels with the name because the report panel has to deep-link to the original
  source, and doing that join per route would eventually drift.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.candidates.models import Candidate, CandidateProfile


@dataclass(frozen=True, slots=True)
class CandidateDisplaySummary:
    """Name plus the few fields a ranking row or report heading needs."""

    profile_id: UUID
    candidate_id: UUID
    document_id: UUID
    display_name: str
    normalized_skills: list[str]
    years_experience: float | None
    education_level: str | None


def summarise_profiles(
    rows: Iterable[tuple[CandidateProfile, str]],
) -> dict[UUID, CandidateDisplaySummary]:
    """Map ``(profile, display_name)`` rows to summaries keyed by profile id.

    Split out from the query so the projection can be tested without a database:
    the interesting behaviour is the ``Numeric`` → ``float`` conversion and the
    preservation of a null ``years_experience`` as null rather than zero, not the
    SQL.
    """
    summaries: dict[UUID, CandidateDisplaySummary] = {}
    for profile, display_name in rows:
        summaries[profile.id] = CandidateDisplaySummary(
            profile_id=profile.id,
            candidate_id=profile.candidate_id,
            document_id=profile.document_id,
            display_name=display_name,
            normalized_skills=list(profile.normalized_skills),
            years_experience=(
                float(profile.years_experience)
                if profile.years_experience is not None
                else None
            ),
            education_level=profile.education_level,
        )
    return summaries


async def load_display_summaries(
    session: AsyncSession, profile_ids: Sequence[UUID]
) -> dict[UUID, CandidateDisplaySummary]:
    """Summaries for the given profiles, in whatever status they are now.

    An empty id set returns an empty mapping without querying: ``IN ()`` is not
    valid SQL, and a run that recalled no candidates must not depend on how a
    dialect happens to render the empty set.
    """
    if not profile_ids:
        return {}
    result = await session.execute(
        select(CandidateProfile, Candidate.display_name)
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        .where(CandidateProfile.id.in_(profile_ids))
    )
    # ``tuples()`` rather than ``all()``: the projection takes plain
    # ``(profile, display_name)`` pairs, and handing it SQLAlchemy ``Row`` objects
    # would make a pure function depend on the query layer's types.
    return summarise_profiles(result.tuples().all())
