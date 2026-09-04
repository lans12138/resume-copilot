"""Model extraction gateway abstraction.

``ModelGateway`` is the single boundary through which the system asks a model
to turn untrusted resume text into a structured ``CandidateProfileDraft``. The
real Qwen-backed implementation lands in a later IMP; ``FakeModelGateway`` is a
deterministic, key-free stand-in used for local runs, CI, and tests. It performs
lightweight heuristic extraction (contact, skills, education level) and stashes
anything it cannot classify into ``unknown_fields`` rather than guessing.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any, Protocol

from backend.app.candidates.schemas import CandidateProfileDraft, ContactInfo, SkillClaim
from backend.app.documents.parsers import ParsedBlock

# Deterministic skill vocabulary for the fake extractor. Lowercase keys.
_SKILL_KEYWORDS: tuple[str, ...] = (
    "python", "java", "c++", "c#", "go", "rust", "typescript", "javascript",
    "react", "vue", "node", "fastapi", "django", "flask", "spring", "sql",
    "postgresql", "mysql", "mongodb", "redis", "elasticsearch", "docker",
    "kubernetes", "kafka", "rabbitmq", "linux", "numpy", "pandas", "pytorch",
    "tensorflow", "langchain", "langgraph", "prompt", "rag",
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?86)?1[3-9]\d{9}")
_EDU_RE = re.compile(
    r"(博士|硕士|研究生|本科|学士|大专|专科|高中)",
    re.IGNORECASE,
)
_EDU_LEVEL_MAP = {
    "博士": "PHD",
    "硕士": "MASTER",
    "研究生": "MASTER",
    "本科": "BACHELOR",
    "学士": "BACHELOR",
    "大专": "ASSOCIATE",
    "专科": "ASSOCIATE",
    "高中": "HIGH_SCHOOL",
}


class ModelGateway(Protocol):
    """Async contract for structured profile extraction."""

    version: str

    async def extract_profile(
        self, *, full_text: str, blocks: Sequence[ParsedBlock]
    ) -> CandidateProfileDraft:
        """Return a normalized draft; never trust the source text."""
        ...


def normalize_email_hash(email: str | None) -> str | None:
    """Stable, non-reversible hint for potential duplicate detection."""
    if not email:
        return None
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


class FakeModelGateway:
    """Heuristic, dependency-free extractor for local runs and tests."""

    version = "fake-v1"

    async def extract_profile(
        self, *, full_text: str, blocks: Sequence[ParsedBlock]
    ) -> CandidateProfileDraft:
        text = (full_text or "").strip()
        if not text:
            return CandidateProfileDraft()

        email_match = _EMAIL_RE.search(text)
        phone_match = _PHONE_RE.search(text)

        skills = self._extract_skills(text)
        education_level = self._extract_education_level(text)

        contact = None
        if email_match or phone_match:
            contact = ContactInfo(
                email=email_match.group(0) if email_match else None,
                phone=phone_match.group(0) if phone_match else None,
            )

        unknown_fields: dict[str, Any] = {}
        # Keep a bounded raw signal so reviewers can recover unparsed context.
        if len(text) <= 4000:
            unknown_fields["raw_text"] = text

        return CandidateProfileDraft(
            full_name=self._extract_name(text),
            contact=contact,
            skills=skills,
            education_level=education_level,
            unknown_fields=unknown_fields,
        )

    @staticmethod
    def _extract_skills(text: str) -> list[SkillClaim]:
        lowered = text.lower()
        found: dict[str, SkillClaim] = {}
        for keyword in _SKILL_KEYWORDS:
            if re.search(rf"(?<![A-Za-z]){re.escape(keyword)}(?![A-Za-z])", lowered):
                found[keyword] = SkillClaim(name=keyword)
        return list(found.values())

    @staticmethod
    def _extract_education_level(text: str) -> str | None:
        match = _EDU_RE.search(text)
        if not match:
            return None
        return _EDU_LEVEL_MAP.get(match.group(1))

    @staticmethod
    def _extract_name(text: str) -> str | None:
        # Very small heuristic: a line that looks like a name (2-4 CJK chars or
        # a Latin "First Last") near the top of the document.
        for line in text.splitlines()[:12]:
            line = line.strip()
            if not line:
                continue
            cjk = re.findall(r"[\u4e00-\u9fff]", line)
            if 2 <= len(cjk) <= 4 and len(line) <= 12:
                return line
            latin = re.fullmatch(r"[A-Za-z]+ [A-Za-z]+", line)
            if latin:
                return line
        return None
