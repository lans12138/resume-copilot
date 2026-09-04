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

from backend.app.core.settings import Settings


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


class HardRuleOutcome(StrEnum):
    """Verdict a single hard rule or the aggregated profile can take.

    ``UNKNOWN`` is reserved for missing evidence and must never be guessed as
    ``FAIL`` (detailed design §8.4). ``FAIL`` does not delete the candidate:
    it still flows into evidence-scored evaluation downstream.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class HardRuleId(StrEnum):
    """The three hard rules consumed by IMP-015."""

    YEARS_EXPERIENCE = "years_experience"
    REQUIRED_EDUCATION = "required_education"
    REQUIRED_SKILLS = "required_skills"


@dataclass(frozen=True)
class HardRuleResult:
    """One rule's verdict over confirmed profile fields (§8.4)."""

    rule_id: HardRuleId
    result: HardRuleOutcome
    reason_code: str
    observed_value: object | None = None
    required_value: object | None = None
    evidence_chunk_ids: list[UUID] = field(default_factory=list)


@dataclass(frozen=True)
class HardRuleBundle:
    """All hard-rule verdicts for one candidate plus the aggregate outcome."""

    rules: list[HardRuleResult]
    overall: HardRuleOutcome


@dataclass(frozen=True)
class RetrievalConfig:
    """Runtime retrieval knobs, frozen at run time into the ranking snapshot.

    Constructed from ``Settings`` (``from_settings``) so the MatchRun snapshot
    captures exactly the weights/RRF_K/Top-K that produced a given ranking.
    """

    structured_weight: float
    keyword_weight: float
    vector_weight: float
    rrf_k: int
    top_k: int
    rule_version: str
    channels: tuple[ChannelName, ...] = (
        ChannelName.STRUCTURED,
        ChannelName.KEYWORD,
        ChannelName.VECTOR,
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> RetrievalConfig:
        return cls(
            structured_weight=settings.structured_weight,
            keyword_weight=settings.keyword_weight,
            vector_weight=settings.vector_weight,
            rrf_k=settings.rrf_k,
            top_k=settings.top_k,
            rule_version=settings.rule_version,
        )


@dataclass(frozen=True)
class FusedCandidate:
    """One candidate after RRF fusion, before/independent of hard-rule fill."""

    candidate_profile_id: UUID
    snapshot_order: int  # 1-based stable order after fusion
    rrf_score: float
    structured_rank: int | None = None
    structured_score: float | None = None
    keyword_rank: int | None = None
    keyword_score: float | None = None
    vector_rank: int | None = None
    vector_score: float | None = None
    hard_rule: HardRuleBundle | None = None


@dataclass(frozen=True)
class RankingSnapshot:
    """Frozen, reproducible ranking written to MatchRunCandidate in IMP-019.

    Holds every channel rank/score, the config that produced it, and the stable
    order. ``FAIL``/``UNKNOWN`` candidates are retained here — filtering is a
    presentation concern owned by the candidate list UI, never by retrieval.
    """

    job_version_id: UUID
    config: RetrievalConfig
    fused: list[FusedCandidate]
