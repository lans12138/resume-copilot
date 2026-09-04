"""IMP-015 verification: RRF fusion, hard rules, ranking snapshot (D13/D14).

RRF and hard rules are pure functions, so every case asserts byte-for-byte
reproducible output for fixed inputs — the property the Golden Dataset relies on.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from backend.app.retrieval.fusion import fuse
from backend.app.retrieval.hard_rules import evaluate_hard_rules
from backend.app.retrieval.models import (
    HardRuleBundle,
    HardRuleId,
    HardRuleOutcome,
    JobQuery,
    RankingSnapshot,
    ReadyProfile,
    RecallBundle,
    RecallHit,
    RetrievalConfig,
)
from backend.app.retrieval.ranking import build_ranking_snapshot

JOB_ID = uuid4()


def _config(**overrides: object) -> RetrievalConfig:
    base = dict(
        structured_weight=1.0,
        keyword_weight=1.0,
        vector_weight=1.0,
        rrf_k=60,
        top_k=10,
        rule_version="v1",
    )
    base.update(overrides)
    return RetrievalConfig(**base)  # type: ignore[arg-type]


def _hit(profile_id: UUID, score: float, rank: int) -> RecallHit:
    return RecallHit(profile_id=profile_id, score=score, rank=rank)


def _bundle(
    structured: list[RecallHit] | None = None,
    keyword: list[RecallHit] | None = None,
    vector: list[RecallHit] | None = None,
) -> RecallBundle:
    return RecallBundle(
        job_version_id=JOB_ID,
        structured=structured or [],
        keyword=keyword or [],
        vector=vector or [],
    )


def _job(**overrides: object) -> JobQuery:
    base = dict(
        job_version_id=JOB_ID,
        required_skills=["python"],
        preferred_skills=[],
        min_years=3.0,
        required_education="本科",
        description_text="python backend",
        requirements_json={},
    )
    base.update(overrides)
    return JobQuery(**base)  # type: ignore[arg-type]


def _profile(
    profile_id: UUID,
    skills: list[str] | None = None,
    years: float | None = 5.0,
    edu: str | None = "本科",
) -> ReadyProfile:
    return ReadyProfile(
        profile_id=profile_id,
        normalized_skills=skills if skills is not None else ["python"],
        years_experience=years,
        education_level=edu,
        profile_json={},
    )


# --- RRF fusion (D13) ---------------------------------------------------------


def test_fusion_ranks_single_channel_by_score() -> None:
    p1, p2 = uuid4(), uuid4()
    bundle = _bundle(structured=[_hit(p1, 0.6, 1), _hit(p2, 0.2, 2)])
    fused = fuse(bundle, _config())
    assert [c.candidate_profile_id for c in fused] == [p1, p2]
    assert fused[0].rrf_score > fused[1].rrf_score
    assert fused[0].structured_rank == 1 and fused[1].structured_rank == 2


def test_fusion_tie_breaks_by_profile_id() -> None:
    p_low = UUID("00000000-0000-0000-0000-00000000000a")
    p_high = UUID("00000000-0000-0000-0000-00000000000b")
    # symmetric: each ranked #1 in a different channel, equal weight -> equal score
    bundle = _bundle(
        structured=[_hit(p_low, 1.0, 1)],
        keyword=[_hit(p_high, 1.0, 1)],
    )
    fused = fuse(bundle, _config())
    assert [c.candidate_profile_id for c in fused] == [p_low, p_high]


def test_fusion_excludes_profiles_absent_from_all_channels() -> None:
    p_hit, p_orphan = uuid4(), uuid4()
    # p_orphan is in no channel, so it never enters the fused candidate set
    bundle = _bundle(structured=[_hit(p_hit, 1.0, 1)])
    fused = fuse(bundle, _config())
    assert [c.candidate_profile_id for c in fused] == [p_hit]
    assert p_orphan not in {c.candidate_profile_id for c in fused}


def test_fusion_respects_top_k_and_orders() -> None:
    profiles = [uuid4() for _ in range(12)]
    hits = [_hit(pid, 1.0 - i * 0.01, i + 1) for i, pid in enumerate(profiles)]
    fused = fuse(_bundle(structured=hits), _config(top_k=3))
    assert len(fused) == 3
    assert [c.snapshot_order for c in fused] == [1, 2, 3]
    assert [c.candidate_profile_id for c in fused] == profiles[:3]


def test_fusion_weight_changes_order() -> None:
    p_struct, p_vector = uuid4(), uuid4()
    bundle = _bundle(
        structured=[_hit(p_struct, 1.0, 1)],
        vector=[_hit(p_vector, 1.0, 1)],
    )
    # equal weights -> both rank, order decided by profile_id (not asserted here)
    assert {c.candidate_profile_id for c in fuse(bundle, _config())} == {
        p_struct,
        p_vector,
    }
    # heavy vector weight makes p_vector win deterministically
    assert [c.candidate_profile_id for c in fuse(bundle, _config(vector_weight=5.0))] == [
        p_vector,
        p_struct,
    ]


def test_ranking_snapshot_carries_config() -> None:
    p1 = uuid4()
    snapshot: RankingSnapshot = build_ranking_snapshot(
        _bundle(structured=[_hit(p1, 1.0, 1)]),
        _config(structured_weight=2.0, rrf_k=42, top_k=7, rule_version="v2"),
        _job(),
        [_profile(p1)],
    )
    assert snapshot.config.structured_weight == 2.0
    assert snapshot.config.rrf_k == 42
    assert snapshot.config.top_k == 7
    assert snapshot.config.rule_version == "v2"


# --- Hard rules (D14) ----------------------------------------------------------


def test_hard_rules_all_pass() -> None:
    job = _job(min_years=3.0, required_education="本科", required_skills=["python"])
    profile = _profile(uuid4(), skills=["python"], years=5.0, edu="本科")
    bundle: HardRuleBundle = evaluate_hard_rules(job, profile)
    assert bundle.overall == HardRuleOutcome.PASS
    assert all(r.result == HardRuleOutcome.PASS for r in bundle.rules)
    assert all(r.evidence_chunk_ids == [] for r in bundle.rules)


def test_hard_rules_years_below_fails() -> None:
    job = _job(min_years=3.0)
    bundle = evaluate_hard_rules(job, _profile(uuid4(), years=2.0))
    years = next(r for r in bundle.rules if r.rule_id == HardRuleId.YEARS_EXPERIENCE)
    assert years.result == HardRuleOutcome.FAIL
    assert years.reason_code == "BELOW_MINIMUM"
    assert bundle.overall == HardRuleOutcome.FAIL


def test_hard_rules_education_below_fails() -> None:
    job = _job(required_education="本科")
    bundle = evaluate_hard_rules(job, _profile(uuid4(), edu="大专"))
    edu = next(r for r in bundle.rules if r.rule_id == HardRuleId.REQUIRED_EDUCATION)
    assert edu.result == HardRuleOutcome.FAIL
    assert bundle.overall == HardRuleOutcome.FAIL


def test_hard_rules_missing_skills_fails() -> None:
    job = _job(required_skills=["python"])
    bundle = evaluate_hard_rules(job, _profile(uuid4(), skills=["java"]))
    skills = next(r for r in bundle.rules if r.rule_id == HardRuleId.REQUIRED_SKILLS)
    assert skills.result == HardRuleOutcome.FAIL
    assert skills.reason_code == "MISSING_SKILLS"
    assert skills.observed_value == ["java"]


def test_hard_rules_missing_field_is_unknown_not_fail() -> None:
    job = _job(min_years=3.0)
    bundle = evaluate_hard_rules(job, _profile(uuid4(), years=None))
    years = next(r for r in bundle.rules if r.rule_id == HardRuleId.YEARS_EXPERIENCE)
    assert years.result == HardRuleOutcome.UNKNOWN
    assert years.reason_code == "MISSING_FIELD"
    # overall must be UNKNOWN, never guessed FAIL
    assert bundle.overall == HardRuleOutcome.UNKNOWN


def test_hard_rules_no_requirement_passes() -> None:
    job = _job(min_years=None, required_education=None, required_skills=[])
    bundle = evaluate_hard_rules(job, _profile(uuid4(), skills=[], years=None, edu=None))
    assert bundle.overall == HardRuleOutcome.PASS
    assert all(r.reason_code == "NO_REQUIREMENT" for r in bundle.rules)


def test_hard_rules_fail_beats_unknown_in_aggregate() -> None:
    # one FAIL and one UNKNOWN -> overall FAIL (FAIL wins, never silently UNKNOWN)
    job = _job(min_years=3.0, required_skills=["python"])
    profile = _profile(uuid4(), skills=["java"], years=None)  # years UNKNOWN, skills FAIL
    bundle = evaluate_hard_rules(job, profile)
    assert bundle.overall == HardRuleOutcome.FAIL


# --- Snapshot retention (FAIL/UNKNOWN not filtered) ----------------------------


def test_fail_candidate_retained_in_snapshot() -> None:
    fail_pid = uuid4()
    pass_pid = uuid4()
    bundle = _bundle(
        structured=[_hit(fail_pid, 1.0, 2), _hit(pass_pid, 1.0, 1)],
    )
    job = _job(min_years=3.0, required_education="本科", required_skills=["python"])
    snapshot = build_ranking_snapshot(
        bundle,
        _config(top_k=10),
        job,
        [
            _profile(pass_pid, skills=["python"], years=5.0, edu="本科"),
            _profile(fail_pid, skills=["java"], years=2.0, edu="大专"),
        ],
    )
    # both retained; the failing one is NOT dropped
    assert len(snapshot.fused) == 2
    fail_c = next(c for c in snapshot.fused if c.candidate_profile_id == fail_pid)
    assert fail_c.hard_rule is not None
    assert fail_c.hard_rule.overall == HardRuleOutcome.FAIL


def test_unknown_candidate_retained_in_snapshot() -> None:
    unknown_pid = uuid4()
    bundle = _bundle(structured=[_hit(unknown_pid, 1.0, 1)])
    job = _job(min_years=3.0)
    snapshot = build_ranking_snapshot(
        bundle, _config(), job, [_profile(unknown_pid, years=None)]
    )
    assert len(snapshot.fused) == 1
    assert snapshot.fused[0].hard_rule is not None
    assert snapshot.fused[0].hard_rule.overall == HardRuleOutcome.UNKNOWN


def test_ranking_reproducible_for_fixed_inputs() -> None:
    p1, p2, p3 = uuid4(), uuid4(), uuid4()
    bundle = _bundle(
        structured=[_hit(p1, 0.9, 1), _hit(p2, 0.4, 2)],
        vector=[_hit(p3, 0.7, 1)],
    )
    job = _job()
    profiles = [_profile(p1), _profile(p2), _profile(p3)]
    cfg = _config()
    first = build_ranking_snapshot(bundle, cfg, job, profiles)
    second = build_ranking_snapshot(bundle, cfg, job, profiles)
    assert [c.candidate_profile_id for c in first.fused] == [
        c.candidate_profile_id for c in second.fused
    ]
    assert [c.rrf_score for c in first.fused] == [c.rrf_score for c in second.fused]
