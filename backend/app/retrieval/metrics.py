"""Retrieval quality metrics over a frozen ``RankingSnapshot`` (IMP-016, §18.2).

All functions here are pure over their inputs: a fixed ``RankingSnapshot`` and a
fixed set of golden-relevant profile ids always yield the same numbers. That
hermetic property is exactly what lets the Golden Dataset regression in
``golden`` assert numeric thresholds (Recall@10 >= 0.85) without any model or
database dependency.

Metrics implemented per detailed design §18.2 and requirement analysis §14.1:
Recall@K, MRR, nDCG@K, plus per-candidate RRF contribution breakdown and
per-channel hit flags.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from uuid import UUID

from backend.app.retrieval.models import (
    ChannelName,
    FusedCandidate,
    RankingSnapshot,
    RetrievalConfig,
)


def _weight_for(channel: ChannelName, config: RetrievalConfig) -> float:
    if channel is ChannelName.STRUCTURED:
        return config.structured_weight
    if channel is ChannelName.KEYWORD:
        return config.keyword_weight
    return config.vector_weight


@dataclass(frozen=True)
class ChannelContribution:
    """Decomposed RRF score of one candidate across the three recall channels."""

    structured: float
    keyword: float
    vector: float

    @property
    def total(self) -> float:
        return self.structured + self.keyword + self.vector


@dataclass(frozen=True)
class CandidateMetric:
    """Per-candidate footprint used by the Golden Dataset report."""

    candidate_profile_id: UUID
    snapshot_order: int
    relevant: bool
    rrf_score: float
    rrf_contribution: ChannelContribution
    structured_hit: bool
    keyword_hit: bool
    vector_hit: bool


@dataclass(frozen=True)
class RetrievalMetrics:
    """Aggregated retrieval-quality metrics for a single Golden Dataset case."""

    job_version_id: UUID
    k: int
    recall_at_k: float
    mrr: float
    ndcg_at_k: float
    num_relevant: int
    num_retrieved: int
    candidates: list[CandidateMetric] = field(default_factory=list)


def rrf_contribution(candidate: FusedCandidate, config: RetrievalConfig) -> ChannelContribution:
    """Split a candidate's fused ``rrf_score`` back into per-channel parts.

    An unhit channel contributes ``0.0``; the sum of the three parts equals the
    stored ``rrf_score`` (the fusion formula in ``fusion.fuse``).
    """
    structured = (
        config.structured_weight / (config.rrf_k + candidate.structured_rank)
        if candidate.structured_rank is not None
        else 0.0
    )
    keyword = (
        config.keyword_weight / (config.rrf_k + candidate.keyword_rank)
        if candidate.keyword_rank is not None
        else 0.0
    )
    vector = (
        config.vector_weight / (config.rrf_k + candidate.vector_rank)
        if candidate.vector_rank is not None
        else 0.0
    )
    return ChannelContribution(structured=structured, keyword=keyword, vector=vector)


def _dcg_at_k(rels: list[int], k: int) -> float:
    total = 0.0
    for position, rel in enumerate(rels[:k], start=1):
        if rel:
            total += 1.0 / math.log2(position + 1)
    return total


def _ndcg_at_k(relevant_flags: list[bool], k: int) -> float:
    rels = [1 if flag else 0 for flag in relevant_flags]
    dcg = _dcg_at_k(rels, k)
    num_relevant = sum(rels)
    ideal = [1] * min(num_relevant, k) + [0] * max(0, k - min(num_relevant, k))
    idcg = _dcg_at_k(ideal, k)
    if idcg == 0.0:
        return 1.0
    return dcg / idcg


def compute_retrieval_metrics(
    snapshot: RankingSnapshot,
    relevant_ids: set[UUID] | frozenset[UUID],
    k: int | None = None,
) -> RetrievalMetrics:
    """Compute Recall@K, MRR, nDCG@K and per-candidate breakdown for one case.

    ``k`` defaults to the snapshot's stored ``config.top_k``. When a case has no
    golden-relevant ids, Recall is vacuously ``1.0`` (nothing missed) and nDCG is
    ``1.0``; MRR is ``0.0`` because no relevant item was ranked.
    """
    effective_k = k if k is not None else snapshot.config.top_k
    relevant = set(relevant_ids)
    ordered = sorted(snapshot.fused, key=lambda cand: cand.snapshot_order)

    candidates = [
        CandidateMetric(
            candidate_profile_id=cand.candidate_profile_id,
            snapshot_order=cand.snapshot_order,
            relevant=cand.candidate_profile_id in relevant,
            rrf_score=cand.rrf_score,
            rrf_contribution=rrf_contribution(cand, snapshot.config),
            structured_hit=cand.structured_rank is not None,
            keyword_hit=cand.keyword_rank is not None,
            vector_hit=cand.vector_rank is not None,
        )
        for cand in ordered
    ]

    num_relevant = len(relevant)
    top = candidates[:effective_k]
    hits = sum(1 for cand in top if cand.relevant)
    recall = hits / num_relevant if num_relevant else 1.0

    first_relevant = next(
        (cand.snapshot_order for cand in candidates if cand.relevant), None
    )
    mrr = 1.0 / first_relevant if first_relevant is not None else 0.0

    flags = [cand.relevant for cand in candidates]
    ndcg = _ndcg_at_k(flags, effective_k)

    return RetrievalMetrics(
        job_version_id=snapshot.job_version_id,
        k=effective_k,
        recall_at_k=recall,
        mrr=mrr,
        ndcg_at_k=ndcg,
        num_relevant=num_relevant,
        num_retrieved=len(ordered),
        candidates=candidates,
    )
