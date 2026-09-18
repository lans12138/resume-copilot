"""Ordinal education scale shared by structured recall and hard rules (PORT-002).

``CandidateProfile.education_level`` is a closed *English* enum
(``HIGH_SCHOOL`` … ``PHD``; see ``candidates.schemas.EDUCATION_LEVELS``), while a
job's requirement carries whatever the recruiter typed — in the synthetic corpus
that is Chinese (``"本科"``). Structured recall and hard rules each used to look
up their own Chinese-only table, so a profile holding ``MASTER`` never matched:
every education hard rule silently degraded to ``UNKNOWN`` and the structured
education component stayed pinned at ``0``. Neither outcome surfaced as an error
— the candidate just quietly lost the education signal. The unit tests did not
catch it either, because they build ``ReadyProfile`` directly with Chinese
values and so bypass the schema that enforces the English enum.

One normalized table is what makes an education verdict comparable at all, and
it is the prerequisite for binding an education claim to the sentence that
supports it.
"""

from __future__ import annotations

# Single source of truth: canonical token, ordinal rank, accepted spellings.
# ``OTHER`` is deliberately absent — it carries no ordinal meaning, so it must
# stay unranked rather than being guessed into a level. ``UNKNOWN_EDUCATION_SCALE``
# in ``hard_rules`` is the reason code for exactly that case.
_EDUCATION_LEVELS: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    ("high_school", 1, ("高中", "中专", "high school")),
    ("associate", 2, ("大专", "专科", "associate")),
    ("bachelor", 3, ("本科", "学士", "bachelor")),
    ("master", 4, ("硕士", "研究生", "master")),
    ("phd", 5, ("博士", "phd", "doctorate")),
)

EDUCATION_RANK: dict[str, int] = {
    canonical: rank for canonical, rank, _ in _EDUCATION_LEVELS
}

# Canonical token -> spellings that may appear in resume text, used to bind an
# education claim to the sentence stating it (reports.evidence_binding).
EDUCATION_SURFACE_FORMS: dict[str, tuple[str, ...]] = {
    canonical: forms for canonical, _, forms in _EDUCATION_LEVELS
}

_SURFACE_TO_CANONICAL: dict[str, str] = {
    form.casefold(): canonical
    for canonical, _, forms in _EDUCATION_LEVELS
    for form in (*forms, canonical)
}


def education_canonical(value: str | None) -> str | None:
    """Normalize either spelling to the canonical token, or ``None`` if unknown."""
    if value is None:
        return None
    return _SURFACE_TO_CANONICAL.get(value.strip().casefold())


def education_rank(value: str | None) -> int | None:
    """Return the ordinal rank for either spelling, or ``None`` when unmapped."""
    canonical = education_canonical(value)
    if canonical is None:
        return None
    return EDUCATION_RANK.get(canonical)
