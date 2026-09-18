"""Bind a report claim to the verbatim excerpt that actually supports it (PORT-002).

The report builder used to cite ``chunks[0]`` for *every* SUPPORTED claim. That
made the §9.4 check pass (the reference was legal and owned) while proving
nothing: a claim reading "候选人满足全部硬性条件" could be pinned to a sentence
about something else entirely, and a reader following the citation would land on
unrelated text. Legality and relevance are different properties, and only the
first one was being enforced.

This module supplies the second one. It locates the *observed value* the rule
already computed — an education level, a skill name, a number of years — inside
the candidate's own chunks, and emits a code-point-exact excerpt around each
occurrence. A claim whose value cannot be found gets no excerpt at all, which is
what lets the caller downgrade its support level instead of inventing a citation.

``section_type`` is deliberately *not* used as a filter. It is free text
(``skill`` / ``skills`` / ``技能`` / ``experience`` all appear in this repo), so a
hard filter on it would silently drop correct evidence. Matching the value is
both stricter and spelling-independent.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from uuid import UUID, uuid4

from backend.app.candidates.models import EvidenceChunk
from backend.app.reports.models import ClaimEvidence
from backend.app.retrieval.education import EDUCATION_SURFACE_FORMS, education_canonical
from backend.app.retrieval.models import HardRuleId

# Upper bound on excerpts per claim. A claim is a single statement; past a few
# citations the extra rows stop informing the reader and start inflating the
# evidence count the evaluation gate reports.
MAX_EVIDENCE_PER_CLAIM = 3

# "5 年" / "5年" / "5.5 年" — the numeric spelling of an experience duration.
_YEARS_RE = re.compile(r"(?<![0-9.])(\d+(?:\.\d+)?)\s*年")

# Single-character Chinese numerals, enough for the durations a resume states.
_CN_NUMERALS: dict[str, float] = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_CN_YEARS_RE = re.compile(r"([一二两三四五六七八九十])\s*年")


def _excerpt(chunk: EvidenceChunk, claim_id: UUID, start: int, end: int) -> ClaimEvidence:
    """Pin one code-point-exact slice of ``chunk.text`` to ``claim_id``."""
    return ClaimEvidence(
        id=uuid4(),
        claim_id=claim_id,
        evidence_chunk_id=chunk.id,
        quote_text=chunk.text[start:end],
        quote_start=start,
        quote_end=end,
    )


def _skill_spans(text: str, skill: str) -> list[tuple[int, int]]:
    """Locate whole-word occurrences of one skill name.

    Boundaries are alphanumeric-only so punctuated names (``c++``, ``c#``,
    ``node.js``) still match, while ``java`` does not match inside ``javascript``.
    """
    needle = skill.strip().casefold()
    if not needle:
        return []
    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])")
    return [(match.start(), match.end()) for match in pattern.finditer(text.casefold())]


def _education_spans(text: str, level: str) -> list[tuple[int, int]]:
    """Locate the spellings of one education level, longest form first."""
    canonical = education_canonical(level)
    if canonical is None:
        return []
    lowered = text.casefold()
    spans: list[tuple[int, int]] = []
    for form in sorted(EDUCATION_SURFACE_FORMS.get(canonical, ()), key=len, reverse=True):
        needle = form.casefold()
        start = lowered.find(needle)
        while start != -1:
            spans.append((start, start + len(needle)))
            start = lowered.find(needle, start + len(needle))
    return spans


def _years_spans(text: str, years: float) -> list[tuple[int, int]]:
    """Locate a stated duration that equals the rule's observed value.

    Only an exact match counts. A resume saying "3 年" does not support a claim
    about 5 years, and rounding the two together is precisely the kind of silent
    over-claiming this module exists to prevent.
    """
    spans: list[tuple[int, int]] = []
    for match in _YEARS_RE.finditer(text):
        if abs(float(match.group(1)) - years) < 0.01:
            spans.append((match.start(), match.end()))
    for match in _CN_YEARS_RE.finditer(text):
        value = _CN_NUMERALS.get(match.group(1))
        if value is not None and abs(value - years) < 0.01:
            spans.append((match.start(), match.end()))
    return spans


def _spans_for(rule_id: HardRuleId, observed_value: object, text: str) -> list[tuple[int, int]]:
    """Dispatch to the finder for one rule, given its observed value."""
    if observed_value is None:
        return []
    if rule_id == HardRuleId.REQUIRED_SKILLS:
        if not isinstance(observed_value, list):
            return []
        spans: list[tuple[int, int]] = []
        for skill in observed_value:
            if isinstance(skill, str):
                spans.extend(_skill_spans(text, skill))
        return spans
    if rule_id == HardRuleId.REQUIRED_EDUCATION:
        if not isinstance(observed_value, str):
            return []
        return _education_spans(text, observed_value)
    if rule_id == HardRuleId.YEARS_EXPERIENCE:
        if isinstance(observed_value, bool) or not isinstance(observed_value, (int, float)):
            return []
        return _years_spans(text, float(observed_value))
    return []


def bind_evidence(
    *,
    claim_id: UUID,
    rule_id: HardRuleId,
    observed_value: object,
    chunks: Sequence[EvidenceChunk],
) -> list[ClaimEvidence]:
    """Return the excerpts of ``chunks`` that state ``observed_value``.

    Empty when nothing matches — the caller must treat that as "no support",
    never as a reason to fall back to an arbitrary chunk.
    """
    found: list[ClaimEvidence] = []
    for chunk in sorted(chunks, key=lambda item: item.chunk_index):
        for start, end in sorted(set(_spans_for(rule_id, observed_value, chunk.text))):
            if len(found) >= MAX_EVIDENCE_PER_CLAIM:
                return found
            found.append(_excerpt(chunk, claim_id, start, end))
    return found


def merge_evidence(
    claim_id: UUID, groups: Sequence[Sequence[ClaimEvidence]]
) -> list[ClaimEvidence]:
    """Union several claims' excerpts onto one aggregate claim.

    Used by the overall hard-rule claim, whose statement ("满足全部硬性条件")
    is only as good as the individual rules behind it. Re-keys the rows to
    ``claim_id`` and de-duplicates on the (chunk, range) tuple, which is what the
    ``uq_claim_evidences_claim_chunk_range`` constraint enforces.
    """
    merged: list[ClaimEvidence] = []
    seen: set[tuple[UUID, int, int]] = set()
    for group in groups:
        for evidence in group:
            key = (evidence.evidence_chunk_id, evidence.quote_start, evidence.quote_end)
            if key in seen:
                continue
            if len(merged) >= MAX_EVIDENCE_PER_CLAIM:
                return merged
            seen.add(key)
            merged.append(
                ClaimEvidence(
                    id=uuid4(),
                    claim_id=claim_id,
                    evidence_chunk_id=evidence.evidence_chunk_id,
                    quote_text=evidence.quote_text,
                    quote_start=evidence.quote_start,
                    quote_end=evidence.quote_end,
                )
            )
    return merged
