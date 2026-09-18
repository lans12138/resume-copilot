"""Hard-rule evaluation over confirmed profile fields (IMP-015, §8.4).

Hard rules consume *confirmed* profile fields only — skills, years, education
that HR already reviewed when flipping the profile to READY. They never read
untrusted document text and never change business state; they just emit
PASS / FAIL / UNKNOWN verdicts.

Key invariant: a missing field is ``UNKNOWN``, never a guessed ``FAIL``. A
``FAIL`` means the confirmed field is present and below the requirement; it does
not delete the candidate (downstream evidence scoring still runs).
"""

from __future__ import annotations

from dataclasses import replace

from backend.app.retrieval.education import education_rank
from backend.app.retrieval.models import (
    FusedCandidate,
    HardRuleBundle,
    HardRuleId,
    HardRuleOutcome,
    HardRuleResult,
    JobQuery,
    ReadyProfile,
)

# Stable reason codes; chosen to be machine-checkable in Golden Dataset reports.
REASON_MEETS_MINIMUM = "MEETS_MINIMUM"
REASON_BELOW_MINIMUM = "BELOW_MINIMUM"
REASON_NO_REQUIREMENT = "NO_REQUIREMENT"
REASON_MISSING_FIELD = "MISSING_FIELD"
REASON_MISSING_SKILLS = "MISSING_SKILLS"
REASON_UNKNOWN_SCALE = "UNKNOWN_EDUCATION_SCALE"


def _years_rule(job: JobQuery, profile: ReadyProfile) -> HardRuleResult:
    if job.min_years is None:
        return HardRuleResult(
            rule_id=HardRuleId.YEARS_EXPERIENCE,
            result=HardRuleOutcome.PASS,
            reason_code=REASON_NO_REQUIREMENT,
            observed_value=profile.years_experience,
            required_value=None,
        )
    if profile.years_experience is None:
        return HardRuleResult(
            rule_id=HardRuleId.YEARS_EXPERIENCE,
            result=HardRuleOutcome.UNKNOWN,
            reason_code=REASON_MISSING_FIELD,
            observed_value=None,
            required_value=job.min_years,
        )
    if profile.years_experience >= job.min_years:
        return HardRuleResult(
            rule_id=HardRuleId.YEARS_EXPERIENCE,
            result=HardRuleOutcome.PASS,
            reason_code=REASON_MEETS_MINIMUM,
            observed_value=profile.years_experience,
            required_value=job.min_years,
        )
    return HardRuleResult(
        rule_id=HardRuleId.YEARS_EXPERIENCE,
        result=HardRuleOutcome.FAIL,
        reason_code=REASON_BELOW_MINIMUM,
        observed_value=profile.years_experience,
        required_value=job.min_years,
    )


def _education_rule(job: JobQuery, profile: ReadyProfile) -> HardRuleResult:
    if job.required_education is None:
        return HardRuleResult(
            rule_id=HardRuleId.REQUIRED_EDUCATION,
            result=HardRuleOutcome.PASS,
            reason_code=REASON_NO_REQUIREMENT,
            observed_value=profile.education_level,
            required_value=None,
        )
    if profile.education_level is None:
        return HardRuleResult(
            rule_id=HardRuleId.REQUIRED_EDUCATION,
            result=HardRuleOutcome.UNKNOWN,
            reason_code=REASON_MISSING_FIELD,
            observed_value=None,
            required_value=job.required_education,
        )
    required_ord = education_rank(job.required_education)
    observed_ord = education_rank(profile.education_level)
    if required_ord is None or observed_ord is None:
        return HardRuleResult(
            rule_id=HardRuleId.REQUIRED_EDUCATION,
            result=HardRuleOutcome.UNKNOWN,
            reason_code=REASON_UNKNOWN_SCALE,
            observed_value=profile.education_level,
            required_value=job.required_education,
        )
    if observed_ord >= required_ord:
        return HardRuleResult(
            rule_id=HardRuleId.REQUIRED_EDUCATION,
            result=HardRuleOutcome.PASS,
            reason_code=REASON_MEETS_MINIMUM,
            observed_value=profile.education_level,
            required_value=job.required_education,
        )
    return HardRuleResult(
        rule_id=HardRuleId.REQUIRED_EDUCATION,
        result=HardRuleOutcome.FAIL,
        reason_code=REASON_BELOW_MINIMUM,
        observed_value=profile.education_level,
        required_value=job.required_education,
    )


def _skills_rule(job: JobQuery, profile: ReadyProfile) -> HardRuleResult:
    if not job.required_skills:
        return HardRuleResult(
            rule_id=HardRuleId.REQUIRED_SKILLS,
            result=HardRuleOutcome.PASS,
            reason_code=REASON_NO_REQUIREMENT,
            observed_value=sorted(profile.normalized_skills),
            required_value=[],
        )
    required = set(job.required_skills)
    have = set(profile.normalized_skills)
    missing = sorted(required - have)
    if not missing:
        return HardRuleResult(
            rule_id=HardRuleId.REQUIRED_SKILLS,
            result=HardRuleOutcome.PASS,
            reason_code=REASON_MEETS_MINIMUM,
            observed_value=sorted(have),
            required_value=sorted(required),
        )
    return HardRuleResult(
        rule_id=HardRuleId.REQUIRED_SKILLS,
        result=HardRuleOutcome.FAIL,
        reason_code=REASON_MISSING_SKILLS,
        observed_value=sorted(have),
        required_value=sorted(required),
    )


def _aggregate(rules: list[HardRuleResult]) -> HardRuleOutcome:
    """FAIL wins (a real violation); UNKNOWN next; PASS only if all pass."""
    outcomes = {rule.result for rule in rules}
    if HardRuleOutcome.FAIL in outcomes:
        return HardRuleOutcome.FAIL
    if HardRuleOutcome.UNKNOWN in outcomes:
        return HardRuleOutcome.UNKNOWN
    return HardRuleOutcome.PASS


def evaluate_hard_rules(job: JobQuery, profile: ReadyProfile) -> HardRuleBundle:
    """Evaluate the three hard rules over confirmed fields; aggregate the verdict."""
    rules = [
        _years_rule(job, profile),
        _education_rule(job, profile),
        _skills_rule(job, profile),
    ]
    return HardRuleBundle(rules=rules, overall=_aggregate(rules))


def attach_hard_rules(
    fused: list[FusedCandidate], job: JobQuery, profiles: list[ReadyProfile]
) -> list[FusedCandidate]:
    """Decorate a fused ranking with per-candidate hard-rule bundles.

    A profile missing from ``profiles`` (should not happen post-recall) keeps
    ``hard_rule=None`` rather than being dropped — fusion order is preserved.
    """
    by_id = {profile.profile_id: profile for profile in profiles}
    enriched: list[FusedCandidate] = []
    for candidate in fused:
        profile = by_id.get(candidate.candidate_profile_id)
        hard_rule = evaluate_hard_rules(job, profile) if profile is not None else None
        enriched.append(replace(candidate, hard_rule=hard_rule))
    return enriched
