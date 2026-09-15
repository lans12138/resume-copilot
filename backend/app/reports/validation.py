"""Evidence-reference verification for report claims (detailed design §9.4).

Every deterministic conclusion in a ``MatchReport`` must cite a verbatim
excerpt from an ``evidence_chunk`` that (1) exists, (2) belongs to the reported
candidate, (3) is sliced inside the chunk's text range, and (4) matches the
slice by Unicode code point. Step 5 (dedup) and step 6 (high-impact downgrade)
live in the report builder so they run exactly once per claim/evidence pair.

These functions are pure so the gate metric "illegal evidence references == 0"
is testable without a database or a model call.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from backend.app.candidates.models import EvidenceChunk
from backend.app.reports.models import ClaimEvidence, ImpactLevel, SupportLevel

# Rewritten text template for a high-impact claim that is not SUPPORTED (§4.5).
INSUFFICIENT_TEMPLATE = "不足以判断该候选人是否满足相关硬性条件"


@dataclass(frozen=True)
class ReferenceVerdict:
    """Outcome of verifying one ``ClaimEvidence`` against its source chunk."""

    evidence: ClaimEvidence
    legal: bool
    reason: str  # empty when legal; otherwise a stable machine key


def verify_evidence_reference(
    *,
    evidence: ClaimEvidence,
    chunk: EvidenceChunk | None,
    candidate_profile_id: UUID,
) -> ReferenceVerdict:
    """Run §9.4 steps 1-4 against a single evidence reference.

    1. the cited chunk must exist;
    2. the chunk must belong to the reported candidate;
    3. the quote range must sit inside the chunk text;
    4. the quoted substring must equal ``quote_text`` code-point for code-point.
    """
    if chunk is None:
        return ReferenceVerdict(evidence, False, "chunk_not_found")
    if chunk.candidate_profile_id != candidate_profile_id:
        return ReferenceVerdict(evidence, False, "ownership_mismatch")
    text = chunk.text
    if not (0 <= evidence.quote_start < evidence.quote_end <= len(text)):
        return ReferenceVerdict(evidence, False, "quote_range_invalid")
    if text[evidence.quote_start : evidence.quote_end] != evidence.quote_text:
        return ReferenceVerdict(evidence, False, "quote_text_mismatch")
    return ReferenceVerdict(evidence, True, "")


def apply_high_impact_guard(
    *,
    impact_level: ImpactLevel,
    support_level: SupportLevel,
    claim_text: str,
) -> tuple[str, SupportLevel]:
    """§4.5 step 6: a HIGH-impact claim that is not SUPPORTED must be rewritten.

    The deterministic "入围 / 淘汰 / 硬条件" wording is replaced by the
    "insufficient evidence" template and the support level is forced to
    INSUFFICIENT so the report never asserts a high-impact verdict it cannot
    back with SUPPORTED evidence.
    """
    if impact_level == ImpactLevel.HIGH and support_level != SupportLevel.SUPPORTED:
        return INSUFFICIENT_TEMPLATE, SupportLevel.INSUFFICIENT
    return claim_text, support_level
