"""Structured recall channel (IMP-014, §8.2).

Deterministic, SQL-condition-like scoring over normalized skills, years of
experience, and education level. The score is a weighted blend in [0, 1]; the
weights are fixed recall-time constants (channel fusion weights live in RRF,
IMP-015). A missing candidate field contributes 0 to that component rather than
penalizing — recall must stay permissive; hard rules (IMP-015) decide PASS/FAIL
on missing evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.app.retrieval.education import education_rank
from backend.app.retrieval.models import JobQuery, ReadyProfile, RecallHit
from backend.app.retrieval.skill_normalization import SkillNormalizer


@dataclass(frozen=True)
class StructuredWeights:
    required: float = 0.4
    preferred: float = 0.2
    years: float = 0.2
    education: float = 0.2


@dataclass
class StructuredRecaller:
    normalizer: SkillNormalizer = field(default_factory=SkillNormalizer)
    weights: StructuredWeights = field(default_factory=StructuredWeights)
    max_results: int = 1000

    def recall(self, job: JobQuery, profiles: list[ReadyProfile]) -> list[RecallHit]:
        scored = [(profile.profile_id, self._score(job, profile)) for profile in profiles]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [
            RecallHit(profile_id=profile_id, score=round(score, 6), rank=index + 1)
            for index, (profile_id, score) in enumerate(scored[: self.max_results])
        ]

    def _score(self, job: JobQuery, profile: ReadyProfile) -> float:
        weights = self.weights
        required = job.required_skills
        required_hit = sum(
            1 for skill in required if self.normalizer.contains(profile.normalized_skills, skill)
        )
        required_ratio = required_hit / len(required) if required else 0.0

        preferred = job.preferred_skills
        preferred_hit = sum(
            1 for skill in preferred if self.normalizer.contains(profile.normalized_skills, skill)
        )
        preferred_ratio = preferred_hit / len(preferred) if preferred else 0.0

        years_score = self._years_score(job.min_years, profile.years_experience)
        education_score = self._education_score(job.required_education, profile.education_level)

        return (
            weights.required * required_ratio
            + weights.preferred * preferred_ratio
            + weights.years * years_score
            + weights.education * education_score
        )

    @staticmethod
    def _years_score(min_years: float | None, years: float | None) -> float:
        if min_years is None:
            return 0.0
        if years is None:
            return 0.0
        if years >= min_years:
            return 1.0
        return max(0.0, min(1.0, years / min_years))

    @staticmethod
    def _education_score(required: str | None, candidate: str | None) -> float:
        if required is None or candidate is None:
            return 0.0
        required_rank = education_rank(required)
        candidate_rank = education_rank(candidate)
        if required_rank is None or candidate_rank is None:
            return 0.0
        if candidate_rank >= required_rank:
            return 1.0
        return candidate_rank / required_rank
