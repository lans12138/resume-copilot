"""Ordinal education scale and the regression it was introduced to fix (PORT-002).

``CandidateProfile.education_level`` is validated against the closed *English*
enum in ``candidates.schemas.EDUCATION_LEVELS`` (``BACHELOR``, ``MASTER``, …),
while a job's ``required_education`` carries whatever the recruiter typed — in
the synthetic corpus that is Chinese (``"本科"``). Structured recall and hard
rules each used to hold their own Chinese-only lookup table, so a profile
holding ``MASTER`` resolved to no rank at all: the education hard rule
permanently degraded to ``UNKNOWN`` and the structured education component stayed
pinned at ``0``. Nothing raised — the candidate just quietly lost the signal.

The pre-existing tests could not catch it: they build ``ReadyProfile`` directly
with Chinese values, bypassing the schema that enforces the English enum. These
tests therefore drive the *schema-valid* spelling through the real rule and
scoring functions.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.app.candidates.schemas import EDUCATION_LEVELS
from backend.app.retrieval.education import (
    EDUCATION_RANK,
    EDUCATION_SURFACE_FORMS,
    education_canonical,
    education_rank,
)
from backend.app.retrieval.hard_rules import (
    REASON_BELOW_MINIMUM,
    REASON_MEETS_MINIMUM,
    REASON_UNKNOWN_SCALE,
    evaluate_hard_rules,
)
from backend.app.retrieval.models import (
    HardRuleId,
    HardRuleOutcome,
    HardRuleResult,
    JobQuery,
    ReadyProfile,
)


def _job(*, required_education: str | None) -> JobQuery:
    return JobQuery(
        job_version_id=uuid4(),
        required_skills=[],
        preferred_skills=[],
        min_years=None,
        required_education=required_education,
        description_text="",
        requirements_json={},
    )


def _profile(*, education_level: str | None) -> ReadyProfile:
    return ReadyProfile(
        profile_id=uuid4(),
        normalized_skills=[],
        years_experience=None,
        education_level=education_level,
        profile_json={},
    )


def _education_result(required: str | None, observed: str | None) -> HardRuleResult:
    bundle = evaluate_hard_rules(
        _job(required_education=required), _profile(education_level=observed)
    )
    return next(rule for rule in bundle.rules if rule.rule_id == HardRuleId.REQUIRED_EDUCATION)


# --------------------------------------------------------------------------- #
# the table itself
# --------------------------------------------------------------------------- #


def test_every_schema_enum_level_is_ranked_except_other() -> None:
    """The scale must cover the schema's enum — that is what makes it usable.

    ``OTHER`` is the deliberate exception: it carries no ordinal meaning, so it
    must stay unranked rather than being guessed into a level.
    """
    for level in EDUCATION_LEVELS:
        if level == "OTHER":
            assert education_rank(level) is None
            assert education_canonical(level) is None
            continue
        assert education_rank(level) is not None, level
        assert education_rank(level) == education_rank(level.lower())


def test_ranks_are_strictly_ordered() -> None:
    ordered = ["HIGH_SCHOOL", "ASSOCIATE", "BACHELOR", "MASTER", "PHD"]
    ranks: list[int] = []
    for level in ordered:
        rank = education_rank(level)
        assert rank is not None, level
        ranks.append(rank)
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("本科", "bachelor"),
        ("学士", "bachelor"),
        ("BACHELOR", "bachelor"),
        (" bachelor ", "bachelor"),
        ("硕士", "master"),
        ("研究生", "master"),
        ("MASTER", "master"),
        ("大专", "associate"),
        ("高中", "high_school"),
        ("博士", "phd"),
        ("PhD", "phd"),
    ],
)
def test_surface_forms_normalize_to_one_canonical_token(surface: str, expected: str) -> None:
    """Chinese, English, and the raw enum spelling must agree on one token."""
    assert education_canonical(surface) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "OTHER", "博士後", "未知"])
def test_unmapped_values_return_none_instead_of_a_guess(value: str | None) -> None:
    assert education_rank(value) is None


def test_surface_forms_are_available_for_evidence_binding() -> None:
    """The report layer needs the spellings to locate a level in resume text."""
    assert "本科" in EDUCATION_SURFACE_FORMS["bachelor"]
    assert set(EDUCATION_SURFACE_FORMS) == set(EDUCATION_RANK)


# --------------------------------------------------------------------------- #
# the regression: an English-enum profile level against a Chinese requirement
# --------------------------------------------------------------------------- #


def test_english_enum_profile_satisfies_a_chinese_requirement() -> None:
    """``MASTER`` (the stored enum) vs ``"本科"`` (the typed requirement) PASSes.

    This is the case that used to be reported as ``UNKNOWN``: the two sides were
    written in different alphabets and neither lookup table bridged them.
    """
    result = _education_result("本科", "MASTER")
    assert result.result == HardRuleOutcome.PASS
    assert result.reason_code == REASON_MEETS_MINIMUM


def test_english_enum_profile_below_a_chinese_requirement_fails() -> None:
    """A genuine shortfall must still be a FAIL, not a silent UNKNOWN."""
    result = _education_result("硕士", "BACHELOR")
    assert result.result == HardRuleOutcome.FAIL
    assert result.reason_code == REASON_BELOW_MINIMUM


def test_both_chinese_sides_still_work() -> None:
    assert _education_result("本科", "硕士").result == HardRuleOutcome.PASS


def test_unmapped_requirement_is_unknown_not_fail() -> None:
    """An unrecognised requirement cannot be judged, so it is never a FAIL."""
    result = _education_result("海归优先", "MASTER")
    assert result.result == HardRuleOutcome.UNKNOWN
    assert result.reason_code == REASON_UNKNOWN_SCALE


def test_missing_profile_level_is_unknown_not_fail() -> None:
    result = _education_result("本科", None)
    assert result.result == HardRuleOutcome.UNKNOWN
    assert result.reason_code == "MISSING_FIELD"
