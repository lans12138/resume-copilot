"""IMP-016 tests: retrieval metrics formulas + Golden Dataset Gate3 regression.

Covers the metric math (Recall@K, MRR, nDCG@K) on controlled inputs and the
hermetic Golden Dataset harness that must keep mean Recall@10 >= 0.85 (Gate3,
requirement analysis §14.1). Also exercises JSON versioning round-trip.
"""

from __future__ import annotations

import math
from pathlib import Path
from uuid import UUID, uuid4

from backend.app.retrieval.golden import (
    BUILTIN_GOLDEN_DATASET,
    GoldenReport,
    build_golden_report,
    evaluate_builtin,
    load_golden_dataset,
    save_golden_dataset,
)
from backend.app.retrieval.metrics import compute_retrieval_metrics, rrf_contribution
from backend.app.retrieval.models import (
    FusedCandidate,
    HardRuleOutcome,
    RankingSnapshot,
    RetrievalConfig,
)


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


def _build_fused(pids: list[UUID]) -> list[FusedCandidate]:
    """Fused candidates with order = index+1 and identical per-channel ranks."""
    return [
        FusedCandidate(
            candidate_profile_id=pid,
            snapshot_order=idx + 1,
            rrf_score=1.0,
            structured_rank=idx + 1,
            keyword_rank=idx + 1,
            vector_rank=idx + 1,
        )
        for idx, pid in enumerate(pids)
    ]


def _snapshot(fused: list[FusedCandidate], config: RetrievalConfig) -> RankingSnapshot:
    return RankingSnapshot(job_version_id=uuid4(), config=config, fused=fused)


def test_recall_mrr_ndcg_perfect() -> None:
    config = _config()
    pids = [uuid4() for _ in range(5)]
    snapshot = _snapshot(_build_fused(pids), config)
    metrics = compute_retrieval_metrics(snapshot, set(pids), k=5)
    assert metrics.recall_at_k == 1.0
    assert metrics.mrr == 1.0
    assert metrics.ndcg_at_k == 1.0
    assert metrics.num_relevant == 5


def test_recall_mrr_ndcg_partial() -> None:
    config = _config()
    pids = [uuid4() for _ in range(10)]
    snapshot = _snapshot(_build_fused(pids), config)
    relevant = {pids[2], pids[6]}  # orders 3 and 7
    metrics = compute_retrieval_metrics(snapshot, relevant, k=10)
    assert metrics.recall_at_k == 1.0
    assert abs(metrics.mrr - 1 / 3) < 1e-9
    expected_dcg = 1 / math.log2(4) + 1 / math.log2(8)
    expected_idcg = 1 / math.log2(2) + 1 / math.log2(3)
    assert abs(metrics.ndcg_at_k - expected_dcg / expected_idcg) < 1e-9


def test_empty_relevant_is_vacuous() -> None:
    config = _config()
    pids = [uuid4() for _ in range(3)]
    snapshot = _snapshot(_build_fused(pids), config)
    metrics = compute_retrieval_metrics(snapshot, set(), k=3)
    assert metrics.recall_at_k == 1.0
    assert metrics.mrr == 0.0
    assert metrics.ndcg_at_k == 1.0


def test_k_truncates_recall_but_not_mrr() -> None:
    config = _config()
    pids = [uuid4() for _ in range(5)]
    snapshot = _snapshot(_build_fused(pids), config)
    metrics = compute_retrieval_metrics(snapshot, {pids[2]}, k=2)  # relevant at order 3
    assert metrics.recall_at_k == 0.0
    assert abs(metrics.mrr - 1 / 3) < 1e-9


def test_rrf_contribution_sums_to_total() -> None:
    config = _config()  # weights 1.0, rrf_k 60
    candidate = FusedCandidate(
        candidate_profile_id=uuid4(),
        snapshot_order=1,
        rrf_score=0.0,
        structured_rank=2,
        keyword_rank=None,
        vector_rank=5,
    )
    contribution = rrf_contribution(candidate, config)
    expected = 1.0 / (60 + 2) + 1.0 / (60 + 5)
    assert abs(contribution.total - expected) < 1e-9
    assert contribution.keyword == 0.0
    assert abs(contribution.structured - 1.0 / 62) < 1e-9
    assert abs(contribution.vector - 1.0 / 65) < 1e-9


def test_builtin_golden_report_passes_gate() -> None:
    config = _config()
    report = evaluate_builtin(config)
    assert isinstance(report, GoldenReport)
    assert report.num_cases == 3
    assert report.recall_at_k_threshold == 0.85
    assert report.mean_recall_at_k >= 0.85
    assert report.recall_at_k_passed is True
    assert report.passed is True

    case1 = next(c for c in report.cases if c.case_id == "case-1")
    # one relevant candidate pushed past Top-K -> Recall@10 = 9/10
    assert abs(case1.metrics.recall_at_k - 0.9) < 1e-9
    # every channel surfaces all relevant candidates in the full bundle
    for key in ("structured", "keyword", "vector"):
        assert case1.channel_recall_of_relevant[key] == 1.0

    dist = report.overall_hard_rule_distribution
    assert set(dist) == {outcome.value for outcome in HardRuleOutcome}
    assert sum(dist.values()) == sum(len(c.metrics.candidates) for c in report.cases)


def test_golden_report_is_reproducible() -> None:
    config = _config()
    first = evaluate_builtin(config)
    second = evaluate_builtin(config)
    assert first.mean_recall_at_k == second.mean_recall_at_k
    assert first.mean_ndcg_at_k == second.mean_ndcg_at_k
    assert first.mean_mrr == second.mean_mrr


def test_golden_json_round_trip(tmp_path: Path) -> None:
    config = _config()
    path = tmp_path / "golden.json"
    save_golden_dataset(BUILTIN_GOLDEN_DATASET, path)
    loaded = load_golden_dataset(path)
    assert loaded.version == BUILTIN_GOLDEN_DATASET.version
    assert len(loaded.cases) == len(BUILTIN_GOLDEN_DATASET.cases)
    report = build_golden_report(loaded, config)
    assert report.mean_recall_at_k >= 0.85
    assert report.passed is True
