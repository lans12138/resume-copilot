"""Skill name normalization and alias resolution (IMP-014, D12).

Candidate and job skills are stored case-insensitively, but product synonyms
("后端" vs "backend", "golang" vs "go") must collapse to one canonical key so
structured and keyword recall match on meaning, not spelling. The alias table
is a product config to be expanded; the normalizer is deterministic so recall
stays reproducible.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# Normalized synonym -> canonical skill key. Keys are stored already normalized
# (strip + casefold); the constructor re-normalizes them defensively.
DEFAULT_SKILL_ALIASES: dict[str, str] = {
    "后端": "backend",
    "后台": "backend",
    "后端开发": "backend",
    "前端": "frontend",
    "前端开发": "frontend",
    "全栈": "fullstack",
    "全栈开发": "fullstack",
    "go语言": "go",
    "golang": "go",
    "机器学习": "machine learning",
    "深度学习": "deep learning",
    "人工智能": "ai",
    "数据科学": "data science",
}


def normalize_skill(value: str) -> str:
    """Strip, casefold, and collapse internal whitespace to one space."""
    return re.sub(r"\s+", " ", value.strip().casefold())


class SkillNormalizer:
    """Maps a raw skill string to its canonical key via an alias table."""

    def __init__(self, aliases: Mapping[str, str] | None = None) -> None:
        source = DEFAULT_SKILL_ALIASES if aliases is None else aliases
        self._aliases = {normalize_skill(k): normalize_skill(v) for k, v in source.items()}

    def canonical(self, value: str) -> str:
        norm = normalize_skill(value)
        return self._aliases.get(norm, norm)

    def contains(self, candidate_skills: list[str], requirement: str) -> bool:
        """True if any candidate skill resolves to the requirement's canonical key."""
        req_canonical = self.canonical(requirement)
        return any(self.canonical(skill) == req_canonical for skill in candidate_skills)
