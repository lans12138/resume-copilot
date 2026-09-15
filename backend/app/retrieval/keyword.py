"""Keyword recall channel (IMP-014, §8.2).

Approximates PostgreSQL text/array matching and ``pg_trgm`` with deterministic
token coverage: the job's required/preferred skills (canonicalized), plus its
description, become query tokens; each candidate's canonical skills plus its
``profile_json`` text become document tokens. Coverage = distinct matched
tokens / distinct query tokens. CJK runs are split into overlapping bigrams so
substring overlap survives tokenization; profiles that match nothing are simply
absent from the channel (they contribute nothing to RRF).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache

from backend.app.retrieval.models import JobQuery, ReadyProfile, RecallHit
from backend.app.retrieval.skill_normalization import SkillNormalizer

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")

DEFAULT_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "or", "of", "to", "in", "for", "with", "a", "an", "on", "at",
        "is", "are", "be", "by", "from", "as", "that", "this", "it", "we", "you",
    }
)


@lru_cache(maxsize=4096)
def _tokenize(text: str) -> frozenset[str]:
    lowered = text.casefold()
    tokens: set[str] = set()
    for match in _TOKEN_RE.finditer(lowered):
        token = match.group(0)
        if token.isascii():
            if token not in DEFAULT_STOPWORDS:
                tokens.add(token)
        elif len(token) == 1:
            tokens.add(token)
        else:
            for index in range(len(token) - 1):
                tokens.add(token[index : index + 2])
    return frozenset(tokens)


@dataclass
class KeywordRecaller:
    normalizer: SkillNormalizer = field(default_factory=SkillNormalizer)
    max_results: int = 1000

    def recall(self, job: JobQuery, profiles: list[ReadyProfile]) -> list[RecallHit]:
        job_tokens = self._job_tokens(job)
        if not job_tokens:
            return []
        scored = [
            (profile.profile_id, self._coverage(job_tokens, self._profile_tokens(profile)))
            for profile in profiles
        ]
        scored = [(profile_id, score) for profile_id, score in scored if score > 0.0]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [
            RecallHit(profile_id=profile_id, score=round(score, 6), rank=index + 1)
            for index, (profile_id, score) in enumerate(scored[: self.max_results])
        ]

    def _job_tokens(self, job: JobQuery) -> frozenset[str]:
        tokens: set[str] = set()
        for skill in (*job.required_skills, *job.preferred_skills):
            tokens.add(self.normalizer.canonical(skill))
        tokens.update(_tokenize(job.description_text))
        return frozenset(tokens)

    def _profile_tokens(self, profile: ReadyProfile) -> frozenset[str]:
        tokens: set[str] = {self.normalizer.canonical(skill) for skill in profile.normalized_skills}
        tokens.update(_tokenize(json.dumps(profile.profile_json, ensure_ascii=False)))
        return frozenset(tokens)

    @staticmethod
    def _coverage(job_tokens: frozenset[str], candidate_tokens: frozenset[str]) -> float:
        if not job_tokens:
            return 0.0
        matched = len(job_tokens & candidate_tokens)
        return matched / len(job_tokens)
