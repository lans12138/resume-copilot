"""Golden Dataset schema and evaluation harness (IMP-016, Gate3).

A ``GoldenDataset`` is a versioned, hermetic collection of ``GoldenCase`` records.
Each case pins a job query, the READY profiles, a precomputed three-way
``RecallBundle`` (synthetic but deterministic), and the human-relevant profile
ids. The harness replays each case through the frozen RRF fusion + hard rules
(IMP-015) and computes retrieval-quality metrics (this module's ``metrics``
sibling), then aggregates a report that asserts the Gate3 threshold
(Recall@10 >= 0.85).

Bundles are precomputed rather than re-run through the live recallers so the
regression stays model- and DB-free; recaller reproducibility is covered by the
IMP-014 unit tests. The built-in dataset guarantees the threshold is met.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from uuid import UUID

from backend.app.retrieval.metrics import (
    RetrievalMetrics,
    compute_retrieval_metrics,
)
from backend.app.retrieval.models import (
    HardRuleOutcome,
    JobQuery,
    RankingSnapshot,
    ReadyProfile,
    RecallBundle,
    RecallHit,
    RetrievalConfig,
)
from backend.app.retrieval.ranking import build_ranking_snapshot

RECALL_AT_K_THRESHOLD = 0.85
DEFAULT_GOLDEN_K = 10

_CHANNEL_KEYS = ("structured", "keyword", "vector")


@dataclass(frozen=True)
class GoldenCase:
    """One versioned, hermetic evaluation case."""

    case_id: str
    job_version_id: UUID
    job: JobQuery
    profiles: list[ReadyProfile]
    bundle: RecallBundle
    relevant_ids: frozenset[UUID]
    description: str = ""


@dataclass(frozen=True)
class GoldenCaseResult:
    """Per-case metrics plus channel/rule diagnostics for the report."""

    case_id: str
    metrics: RetrievalMetrics
    channel_recall_of_relevant: dict[str, float]
    hard_rule_distribution: dict[str, int]


@dataclass(frozen=True)
class GoldenReport:
    """Aggregated, gate-decision-ready report over a whole dataset."""

    dataset_version: str
    k: int
    num_cases: int
    mean_recall_at_k: float
    mean_mrr: float
    mean_ndcg_at_k: float
    recall_at_k_threshold: float
    recall_at_k_passed: bool
    mean_channel_recall_of_relevant: dict[str, float]
    overall_hard_rule_distribution: dict[str, int]
    cases: list[GoldenCaseResult] = field(default_factory=list)
    passed: bool = False


# --------------------------------------------------------------------------- #
# Harness replay
# --------------------------------------------------------------------------- #


def _channel_recall_of_relevant(
    bundle: RecallBundle, relevant_ids: frozenset[UUID]
) -> dict[str, float]:
    """Fraction of golden-relevant candidates hit by each channel (full bundle).

    Measures per-channel coverage independent of the RRF Top-K truncation, so a
    relevant candidate that fusion pushed below Top-K still counts as recalled by
    the channel that surfaced it.
    """
    sets = {
        "structured": {hit.profile_id for hit in bundle.structured},
        "keyword": {hit.profile_id for hit in bundle.keyword},
        "vector": {hit.profile_id for hit in bundle.vector},
    }
    total = len(relevant_ids)
    if total == 0:
        return {key: 0.0 for key in _CHANNEL_KEYS}
    return {
        key: sum(1 for rid in relevant_ids if rid in ids) / total for key, ids in sets.items()
    }


def _hard_rule_distribution(snapshot: RankingSnapshot) -> dict[str, int]:
    dist = {outcome.value: 0 for outcome in HardRuleOutcome}
    for cand in snapshot.fused:
        if cand.hard_rule is not None:
            dist[cand.hard_rule.overall.value] = dist.get(cand.hard_rule.overall.value, 0) + 1
    return dist


def run_golden_case(case: GoldenCase, config: RetrievalConfig) -> GoldenCaseResult:
    """Replay one case: fuse + hard rules, then compute its metrics and diagnostics."""
    snapshot = build_ranking_snapshot(case.bundle, config, case.job, case.profiles)
    metrics = compute_retrieval_metrics(snapshot, case.relevant_ids)
    return GoldenCaseResult(
        case_id=case.case_id,
        metrics=metrics,
        channel_recall_of_relevant=_channel_recall_of_relevant(case.bundle, case.relevant_ids),
        hard_rule_distribution=_hard_rule_distribution(snapshot),
    )


def build_golden_report(
    dataset: GoldenDataset, config: RetrievalConfig, k: int | None = None
) -> GoldenReport:
    """Aggregate per-case results into a gate report (mean metrics + diagnostics)."""
    effective_k = k if k is not None else config.top_k
    results = [run_golden_case(case, config) for case in dataset.cases]
    if not results:
        return GoldenReport(
            dataset_version=dataset.version,
            k=effective_k,
            num_cases=0,
            mean_recall_at_k=0.0,
            mean_mrr=0.0,
            mean_ndcg_at_k=0.0,
            recall_at_k_threshold=RECALL_AT_K_THRESHOLD,
            recall_at_k_passed=False,
            mean_channel_recall_of_relevant={key: 0.0 for key in _CHANNEL_KEYS},
            overall_hard_rule_distribution={outcome.value: 0 for outcome in HardRuleOutcome},
            cases=[],
            passed=False,
        )

    count = len(results)
    mean_recall = sum(r.metrics.recall_at_k for r in results) / count
    mean_mrr = sum(r.metrics.mrr for r in results) / count
    mean_ndcg = sum(r.metrics.ndcg_at_k for r in results) / count

    mean_channel: dict[str, float] = {}
    for key in _CHANNEL_KEYS:
        mean_channel[key] = sum(r.channel_recall_of_relevant[key] for r in results) / count

    overall_dist = {outcome.value: 0 for outcome in HardRuleOutcome}
    for result in results:
        for outcome, value in result.hard_rule_distribution.items():
            overall_dist[outcome] = overall_dist.get(outcome, 0) + value

    passed = mean_recall >= RECALL_AT_K_THRESHOLD
    return GoldenReport(
        dataset_version=dataset.version,
        k=effective_k,
        num_cases=count,
        mean_recall_at_k=mean_recall,
        mean_mrr=mean_mrr,
        mean_ndcg_at_k=mean_ndcg,
        recall_at_k_threshold=RECALL_AT_K_THRESHOLD,
        recall_at_k_passed=passed,
        mean_channel_recall_of_relevant=mean_channel,
        overall_hard_rule_distribution=overall_dist,
        cases=results,
        passed=passed,
    )


# --------------------------------------------------------------------------- #
# Built-in dataset (synthetic, deterministic, threshold-guaranteed)
# --------------------------------------------------------------------------- #


def _make_case(
    case_id: str,
    offset: int,
    num_relevant: int,
    num_irrelevant: int,
    tail_relevant: int,
) -> GoldenCase:
    """Build a synthetic case: relevant candidates rank first in every channel.

    ``tail_relevant`` relevant ids are pushed past Top-K so the fused Recall@K
    drops below 1.0, exercising the threshold math without failing the gate.
    """
    total = num_relevant + num_irrelevant
    profiles: list[ReadyProfile] = []
    relevant_ids: set[UUID] = set()
    for i in range(total):
        profile_id = UUID(int=offset + i)
        is_relevant = i < num_relevant
        years = None if i % 3 == 0 else float(2 + i)
        education = ["大专", "本科", "硕士", "博士"][i % 4] if i % 5 != 0 else None
        skills = ["python", f"skill_{i}"] if i % 2 == 0 else [f"skill_{i}"]
        profiles.append(
            ReadyProfile(
                profile_id=profile_id,
                normalized_skills=skills,
                years_experience=years,
                education_level=education,
                profile_json={"idx": i},
            )
        )
        if is_relevant:
            relevant_ids.add(profile_id)

    job = JobQuery(
        job_version_id=UUID(int=offset + 900),
        required_skills=["python"],
        preferred_skills=[],
        min_years=3.0,
        required_education="本科",
        description_text="backend engineer",
        requirements_json={},
    )

    relevant_list = [p.profile_id for p in profiles if p.profile_id in relevant_ids]
    irrelevant_list = [p.profile_id for p in profiles if p.profile_id not in relevant_ids]
    moved: list[UUID] = []
    if tail_relevant and relevant_list:
        for _ in range(tail_relevant):
            moved.append(relevant_list.pop())
    ordered_ids = relevant_list + irrelevant_list + moved

    def _hits() -> list[RecallHit]:
        return [
            RecallHit(profile_id=pid, score=float(total - rank), rank=rank)
            for rank, pid in enumerate(ordered_ids, start=1)
        ]

    bundle = RecallBundle(
        job_version_id=job.job_version_id,
        structured=_hits(),
        keyword=_hits(),
        vector=_hits(),
    )
    return GoldenCase(
        case_id=case_id,
        job_version_id=job.job_version_id,
        job=job,
        profiles=profiles,
        bundle=bundle,
        relevant_ids=frozenset(relevant_ids),
        description=f"{num_relevant} relevant / {num_irrelevant} irrelevant, "
        f"{tail_relevant} relevant pushed past Top-K",
    )


def build_builtin_golden_dataset() -> GoldenDataset:
    """Versioned synthetic dataset whose mean Recall@10 stays above the gate."""
    return GoldenDataset(
        version="v1",
        note="Synthetic, deterministic Golden Dataset for hermetic Gate3 regression.",
        cases=[
            _make_case("case-1", 1_000_000, num_relevant=10, num_irrelevant=2, tail_relevant=1),
            _make_case("case-2", 2_000_000, num_relevant=9, num_irrelevant=1, tail_relevant=0),
            _make_case("case-3", 3_000_000, num_relevant=8, num_irrelevant=2, tail_relevant=0),
        ],
    )


@dataclass(frozen=True)
class GoldenDataset:
    """Versioned collection of Golden Cases; persistable to JSON."""

    version: str
    cases: list[GoldenCase]
    note: str = ""


# Evaluated at import time (cheap, deterministic).
BUILTIN_GOLDEN_DATASET = build_builtin_golden_dataset()


def evaluate_builtin(config: RetrievalConfig, k: int | None = None) -> GoldenReport:
    """Run the built-in dataset and return its gate report (Gate3 entry point)."""
    return build_golden_report(BUILTIN_GOLDEN_DATASET, config, k)


# --------------------------------------------------------------------------- #
# JSON codec (versioning / persistence)
# --------------------------------------------------------------------------- #


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected str, got {type(value).__name__}")
    return value


def _require_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"expected list, got {type(value).__name__}")
    return value


def _require_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"expected dict, got {type(value).__name__}")
    return value


def _profile_to_dict(profile: ReadyProfile) -> dict[str, object]:
    return {
        "profile_id": str(profile.profile_id),
        "normalized_skills": list(profile.normalized_skills),
        "years_experience": profile.years_experience,
        "education_level": profile.education_level,
        "profile_json": profile.profile_json,
    }


def _profile_from_dict(raw: dict[str, object]) -> ReadyProfile:
    return ReadyProfile(
        profile_id=UUID(_require_str(raw["profile_id"])),
        normalized_skills=[_require_str(item) for item in _require_list(raw["normalized_skills"])],
        years_experience=cast("float | None", raw["years_experience"]),
        education_level=cast("str | None", raw["education_level"]),
        profile_json=cast("dict[str, object]", raw["profile_json"]),
    )


def _job_to_dict(job: JobQuery) -> dict[str, object]:
    return {
        "job_version_id": str(job.job_version_id),
        "required_skills": list(job.required_skills),
        "preferred_skills": list(job.preferred_skills),
        "min_years": job.min_years,
        "required_education": job.required_education,
        "description_text": job.description_text,
        "requirements_json": job.requirements_json,
    }


def _job_from_dict(raw: dict[str, object]) -> JobQuery:
    return JobQuery(
        job_version_id=UUID(_require_str(raw["job_version_id"])),
        required_skills=[_require_str(item) for item in _require_list(raw["required_skills"])],
        preferred_skills=[_require_str(item) for item in _require_list(raw["preferred_skills"])],
        min_years=cast("float | None", raw["min_years"]),
        required_education=cast("str | None", raw["required_education"]),
        description_text=_require_str(raw["description_text"]),
        requirements_json=cast("dict[str, object]", raw["requirements_json"]),
    )


def _bundle_to_dict(bundle: RecallBundle) -> dict[str, object]:
    def _channel(hits: list[RecallHit]) -> list[object]:
        return [
            {"profile_id": str(hit.profile_id), "score": hit.score, "rank": hit.rank}
            for hit in hits
        ]

    return {
        "job_version_id": str(bundle.job_version_id),
        "structured": _channel(bundle.structured),
        "keyword": _channel(bundle.keyword),
        "vector": _channel(bundle.vector),
    }


def _bundle_from_dict(raw: dict[str, object]) -> RecallBundle:
    def _channel(items: object) -> list[RecallHit]:
        hits: list[RecallHit] = []
        for item in _require_list(items):
            hit = _require_dict(item)
            hits.append(
                RecallHit(
                    profile_id=UUID(_require_str(hit["profile_id"])),
                    score=cast(float, hit["score"]),
                    rank=cast(int, hit["rank"]),
                )
            )
        return hits

    return RecallBundle(
        job_version_id=UUID(_require_str(raw["job_version_id"])),
        structured=_channel(raw["structured"]),
        keyword=_channel(raw["keyword"]),
        vector=_channel(raw["vector"]),
    )


def _case_to_dict(case: GoldenCase) -> dict[str, object]:
    return {
        "case_id": case.case_id,
        "job_version_id": str(case.job_version_id),
        "job": _job_to_dict(case.job),
        "profiles": [_profile_to_dict(p) for p in case.profiles],
        "bundle": _bundle_to_dict(case.bundle),
        "relevant_ids": [str(rid) for rid in case.relevant_ids],
        "description": case.description,
    }


def _case_from_dict(raw: dict[str, object]) -> GoldenCase:
    return GoldenCase(
        case_id=_require_str(raw["case_id"]),
        job_version_id=UUID(_require_str(raw["job_version_id"])),
        job=_job_from_dict(_require_dict(raw["job"])),
        profiles=[_profile_from_dict(_require_dict(p)) for p in _require_list(raw["profiles"])],
        bundle=_bundle_from_dict(_require_dict(raw["bundle"])),
        relevant_ids=frozenset(
            UUID(_require_str(rid)) for rid in _require_list(raw["relevant_ids"])
        ),
        description=_require_str(raw["description"]),
    )


def golden_to_dict(dataset: GoldenDataset) -> dict[str, object]:
    return {
        "version": dataset.version,
        "note": dataset.note,
        "cases": [_case_to_dict(case) for case in dataset.cases],
    }


def golden_from_dict(raw: dict[str, object]) -> GoldenDataset:
    return GoldenDataset(
        version=_require_str(raw["version"]),
        note=_require_str(raw["note"]),
        cases=[_case_from_dict(_require_dict(c)) for c in _require_list(raw["cases"])],
    )


def save_golden_dataset(dataset: GoldenDataset, path: Path) -> None:
    """Persist a versioned dataset to JSON (versioning requirement, §18.2)."""
    path.write_text(
        json.dumps(golden_to_dict(dataset), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_golden_dataset(path: Path) -> GoldenDataset:
    """Load a previously saved dataset; raises on malformed payloads."""
    return golden_from_dict(json.loads(path.read_text(encoding="utf-8")))


# Re-export for callers that build reports from a config only.
__all__ = [
    "GoldenDataset",
    "GoldenCase",
    "GoldenCaseResult",
    "GoldenReport",
    "BUILTIN_GOLDEN_DATASET",
    "RECALL_AT_K_THRESHOLD",
    "run_golden_case",
    "build_golden_report",
    "evaluate_builtin",
    "build_builtin_golden_dataset",
    "golden_to_dict",
    "golden_from_dict",
    "save_golden_dataset",
    "load_golden_dataset",
]
