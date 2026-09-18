"""Value-located evidence binding (PORT-002).

``build_candidate_report`` used to cite ``chunks[0]`` for every SUPPORTED claim.
The §9.4 check passed — the reference was real and owned — while proving nothing:
a claim about the required skills could be pinned to a sentence about the
candidate's hobbies. Legality and relevance are different properties, and only
legality was enforced.

These tests pin the second one. ``bind_evidence`` must find the *observed value*
the rule already computed inside the candidate's own chunks, must find nothing
when the value is absent, and must never fall back to an arbitrary chunk.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from backend.app.candidates.models import EvidenceChunk
from backend.app.reports.evidence_binding import (
    MAX_EVIDENCE_PER_CLAIM,
    bind_evidence,
    merge_evidence,
)
from backend.app.reports.models import ClaimEvidence
from backend.app.retrieval.models import HardRuleId


def _chunk(profile_id: UUID, text: str, index: int = 0) -> EvidenceChunk:
    return EvidenceChunk(
        id=uuid4(),
        document_id=uuid4(),
        candidate_profile_id=profile_id,
        chunk_index=index,
        section_type="skill",
        locator_json={"page": 1},
        text=text,
        text_sha256="x" * 64,
    )


def _quotes(evidences: list[ClaimEvidence]) -> list[str]:
    return [evidence.quote_text for evidence in evidences]


# --------------------------------------------------------------------------- #
# the excerpt must be a code-point-exact slice of the chunk
# --------------------------------------------------------------------------- #


def test_every_excerpt_is_a_verbatim_slice_of_its_chunk() -> None:
    profile = uuid4()
    chunk = _chunk(profile, "工作经历：5 年 Python 后端开发，熟悉 Docker")
    evidences = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.YEARS_EXPERIENCE,
        observed_value=5.0,
        chunks=[chunk],
    )
    assert _quotes(evidences) == ["5 年"]
    for evidence in evidences:
        assert (
            chunk.text[evidence.quote_start : evidence.quote_end] == evidence.quote_text
        )


# --------------------------------------------------------------------------- #
# years
# --------------------------------------------------------------------------- #


def test_years_matches_the_exact_stated_value() -> None:
    profile = uuid4()
    chunk = _chunk(profile, "5 年经验")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.YEARS_EXPERIENCE,
        observed_value=5.0,
        chunks=[chunk],
    )
    assert _quotes(found) == ["5 年"]


def test_years_does_not_match_a_different_duration() -> None:
    """A resume saying "3 年" does not support a claim about 5 years."""
    profile = uuid4()
    chunk = _chunk(profile, "3 年经验")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.YEARS_EXPERIENCE,
        observed_value=5.0,
        chunks=[chunk],
    )
    assert found == []


def test_years_does_not_match_a_substring_of_a_larger_number() -> None:
    """``15 年`` must not be read as evidence of ``5`` years."""
    profile = uuid4()
    chunk = _chunk(profile, "总计 15 年从业")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.YEARS_EXPERIENCE,
        observed_value=5.0,
        chunks=[chunk],
    )
    assert found == []


def test_years_accepts_a_chinese_numeral() -> None:
    profile = uuid4()
    chunk = _chunk(profile, "五年以上后端经验")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.YEARS_EXPERIENCE,
        observed_value=5.0,
        chunks=[chunk],
    )
    assert _quotes(found) == ["五年"]


def test_years_accepts_a_decimal() -> None:
    profile = uuid4()
    chunk = _chunk(profile, "3.5 年测试经验")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.YEARS_EXPERIENCE,
        observed_value=3.5,
        chunks=[chunk],
    )
    assert _quotes(found) == ["3.5 年"]


@pytest.mark.parametrize("observed", [None, "5", True])
def test_years_ignores_a_value_that_is_not_a_number(observed: object) -> None:
    profile = uuid4()
    chunk = _chunk(profile, "5 年经验")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.YEARS_EXPERIENCE,
        observed_value=observed,
        chunks=[chunk],
    )
    assert found == []


# --------------------------------------------------------------------------- #
# education
# --------------------------------------------------------------------------- #


def test_education_matches_the_chinese_spelling() -> None:
    profile = uuid4()
    chunk = _chunk(profile, "教育背景：本科，计算机科学与技术")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.REQUIRED_EDUCATION,
        observed_value="本科",
        chunks=[chunk],
    )
    assert _quotes(found) == ["本科"]


def test_education_matches_the_english_enum_spelling() -> None:
    """The profile stores ``MASTER``; the resume says ``硕士``."""
    profile = uuid4()
    chunk = _chunk(profile, "学历：硕士")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.REQUIRED_EDUCATION,
        observed_value="MASTER",
        chunks=[chunk],
    )
    assert _quotes(found) == ["硕士"]


def test_education_prefers_the_longest_matching_form() -> None:
    """``研究生`` is a longer spelling of the same level as ``硕士``."""
    profile = uuid4()
    chunk = _chunk(profile, "研究生学历")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.REQUIRED_EDUCATION,
        observed_value="MASTER",
        chunks=[chunk],
    )
    assert _quotes(found) == ["研究生"]


def test_education_does_not_match_a_different_level() -> None:
    profile = uuid4()
    chunk = _chunk(profile, "教育背景：本科")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.REQUIRED_EDUCATION,
        observed_value="硕士",
        chunks=[chunk],
    )
    assert found == []


def test_education_ignores_an_unranked_level() -> None:
    """``OTHER`` has no ordinal meaning, so there is nothing to look for."""
    profile = uuid4()
    chunk = _chunk(profile, "教育背景：其他")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.REQUIRED_EDUCATION,
        observed_value="OTHER",
        chunks=[chunk],
    )
    assert found == []


# --------------------------------------------------------------------------- #
# skills
# --------------------------------------------------------------------------- #


def test_skill_matches_case_insensitively_but_quotes_the_original() -> None:
    profile = uuid4()
    chunk = _chunk(profile, "熟悉 Python 与 Go")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.REQUIRED_SKILLS,
        observed_value=["python"],
        chunks=[chunk],
    )
    assert _quotes(found) == ["Python"]


def test_skill_does_not_match_inside_a_longer_word() -> None:
    """``java`` must not be satisfied by ``javascript``."""
    profile = uuid4()
    chunk = _chunk(profile, "熟悉 JavaScript")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.REQUIRED_SKILLS,
        observed_value=["java"],
        chunks=[chunk],
    )
    assert found == []


@pytest.mark.parametrize("skill", ["c++", "c#", "node.js", ".net"])
def test_punctuated_skill_names_still_match(skill: str) -> None:
    """Boundaries are alphanumeric-only, so punctuation inside a name is fine."""
    profile = uuid4()
    chunk = _chunk(profile, f"技术栈包含 {skill} 与 Docker")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.REQUIRED_SKILLS,
        observed_value=[skill],
        chunks=[chunk],
    )
    assert _quotes(found) == [skill]


def test_one_claim_can_cite_several_skills() -> None:
    profile = uuid4()
    chunk = _chunk(profile, "熟悉 Python、Docker 与 Kubernetes")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.REQUIRED_SKILLS,
        observed_value=["python", "docker"],
        chunks=[chunk],
    )
    assert sorted(_quotes(found)) == ["Docker", "Python"]


def test_a_missing_skill_yields_no_excerpt_for_it() -> None:
    """Only the skills actually present are cited — the absent one is not guessed."""
    profile = uuid4()
    chunk = _chunk(profile, "熟悉 Python")
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.REQUIRED_SKILLS,
        observed_value=["python", "kubernetes"],
        chunks=[chunk],
    )
    assert _quotes(found) == ["Python"]


# --------------------------------------------------------------------------- #
# ordering, cap, and the empty case
# --------------------------------------------------------------------------- #


def test_excerpts_follow_chunk_order_not_input_order() -> None:
    profile = uuid4()
    first = _chunk(profile, "5 年经验", index=0)
    second = _chunk(profile, "另计 5 年实习", index=1)
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.YEARS_EXPERIENCE,
        observed_value=5.0,
        chunks=[second, first],  # deliberately reversed
    )
    assert [evidence.evidence_chunk_id for evidence in found] == [first.id, second.id]


def test_excerpts_are_capped_per_claim() -> None:
    """A claim is one statement; past a few citations the rows stop informing."""
    profile = uuid4()
    chunks = [_chunk(profile, "5 年经验", index=index) for index in range(10)]
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.YEARS_EXPERIENCE,
        observed_value=5.0,
        chunks=chunks,
    )
    assert len(found) == MAX_EVIDENCE_PER_CLAIM


def test_a_claim_with_nothing_to_cite_gets_no_excerpt() -> None:
    """The critical property: no match means no citation, never a fallback."""
    profile = uuid4()
    chunks = [
        _chunk(profile, "个人爱好：马拉松、摄影", index=0),
        _chunk(profile, "教育背景：本科", index=1),
    ]
    found = bind_evidence(
        claim_id=uuid4(),
        rule_id=HardRuleId.YEARS_EXPERIENCE,
        observed_value=5.0,
        chunks=chunks,
    )
    assert found == []


def test_every_excerpt_carries_the_requested_claim_id() -> None:
    profile = uuid4()
    chunk = _chunk(profile, "5 年经验")
    claim_id = uuid4()
    found = bind_evidence(
        claim_id=claim_id,
        rule_id=HardRuleId.YEARS_EXPERIENCE,
        observed_value=5.0,
        chunks=[chunk],
    )
    assert all(evidence.claim_id == claim_id for evidence in found)


# --------------------------------------------------------------------------- #
# merge_evidence — the aggregate claim's union
# --------------------------------------------------------------------------- #


def _evidence(chunk: EvidenceChunk, start: int, end: int) -> ClaimEvidence:
    return ClaimEvidence(
        id=uuid4(),
        claim_id=uuid4(),
        evidence_chunk_id=chunk.id,
        quote_text=chunk.text[start:end],
        quote_start=start,
        quote_end=end,
    )


def test_merge_rekeys_every_row_to_the_aggregate_claim() -> None:
    profile = uuid4()
    chunk = _chunk(profile, "5 年经验")
    aggregate_id = uuid4()
    merged = merge_evidence(aggregate_id, [[_evidence(chunk, 0, 3)]])
    assert [evidence.claim_id for evidence in merged] == [aggregate_id]


def test_merge_deduplicates_identical_ranges() -> None:
    """``uq_claim_evidences_claim_chunk_range`` forbids the duplicate."""
    profile = uuid4()
    chunk = _chunk(profile, "5 年经验")
    same_range = [_evidence(chunk, 0, 3), _evidence(chunk, 0, 3)]
    merged = merge_evidence(uuid4(), [same_range, same_range])
    assert len(merged) == 1


def test_merge_keeps_distinct_ranges_of_the_same_chunk() -> None:
    profile = uuid4()
    chunk = _chunk(profile, "5 年经验，另有 5 年实习")
    merged = merge_evidence(uuid4(), [[_evidence(chunk, 0, 3), _evidence(chunk, 9, 12)]])
    assert len(merged) == 2


def test_merge_of_nothing_is_empty() -> None:
    assert merge_evidence(uuid4(), []) == []
    assert merge_evidence(uuid4(), [[], []]) == []


def test_merge_is_capped_per_claim() -> None:
    profile = uuid4()
    chunk = _chunk(profile, "0123456789")
    groups = [[_evidence(chunk, index, index + 1) for index in range(10)]]
    assert len(merge_evidence(uuid4(), groups)) == MAX_EVIDENCE_PER_CLAIM
