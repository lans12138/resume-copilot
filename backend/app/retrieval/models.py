"""Retrieval domain types for the three-way recall (IMP-014).

A ``RetrievalQuery`` pins a job version, the profile status filter (always
READY), the channel set, and the final top-k. Each recaller returns a ranked
list of ``RecallHit`` (profile_id + score + 1-based rank); the RRF fusion in
IMP-015 consumes those rankings. Recalling is deterministic for fixed inputs:
the same job version and the same READY profile corpus always produce the same
ranks, which is what makes the Golden Dataset reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class ChannelName(StrEnum):
    """The three recall channels fused by RRF in IMP-015."""

    STRUCTURED = "structured"
    KEYWORD = "keyword"
    VECTOR = "vector"


@dataclass(frozen=True)
class RecallHit:
    """One profile's rank inside a single channel."""

    profile_id: UUID
    score: float
    rank: int  # 1-based rank within the channel


@dataclass(frozen=True)
class ReadyProfile:
    """Retrieval-relevant projection of a READY ``CandidateProfile``."""

    profile_id: UUID
    normalized_skills: list[str]
    years_experience: float | None
    education_level: str | None
    profile_json: dict[str, object]


@dataclass(frozen=True)
class ChunkVector:
    """One evidence chunk's embedding, bound to its profile."""

    candidate_profile_id: UUID
    embedding: list[float]


@dataclass(frozen=True)
class JobQuery:
    """Retrieval-relevant projection of a ``JobVersion``."""

    job_version_id: UUID
    required_skills: list[str]
    preferred_skills: list[str]
    min_years: float | None
    required_education: str | None
    description_text: str
    requirements_json: dict[str, object]

    @property
    def search_text(self) -> str:
        """Text fed to the embedding gateway to produce the job vector."""
        parts = [self.description_text]
        parts.extend(self.required_skills)
        parts.extend(self.preferred_skills)
        return "\n".join(part for part in parts if part)


@dataclass(frozen=True)
class RetrievalQuery:
    """Immutable recall request; never re-reads the live Job row."""

    job_version_id: UUID
    top_k: int
    rule_version: str
    channels: tuple[ChannelName, ...] = (
        ChannelName.STRUCTURED,
        ChannelName.KEYWORD,
        ChannelName.VECTOR,
    )


@dataclass
class RecallBundle:
    """Per-channel ranked hits for one job version (RRF input, IMP-015)."""

    job_version_id: UUID
    structured: list[RecallHit] = field(default_factory=list)
    keyword: list[RecallHit] = field(default_factory=list)
    vector: list[RecallHit] = field(default_factory=list)
