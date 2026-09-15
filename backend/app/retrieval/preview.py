"""Candidate ranking preview for the list UI (IMP-017).

``preview_ranking`` is the presentation-layer coordinator: it runs the same
three-way recall + RRF fusion + hard rules used by a MatchRun, then joins the
human-readable profile view and applies the *explicit* client filter. The
filter only narrows what the UI shows — ``total`` always reports the full Top-K
size, so filtering can never shrink the MatchRun candidate set (detailed design
§8.4, §15.4: "筛选不改变 MatchRun 范围").
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from backend.app.infrastructure.embedding import EmbeddingGateway
from backend.app.retrieval.models import (
    ChannelName,
    HardRuleBundle,
    HardRuleOutcome,
    ReadyProfileView,
    RetrievalConfig,
    RetrievalQuery,
)
from backend.app.retrieval.ranking import build_ranking_snapshot
from backend.app.retrieval.repository import RetrievalRepository
from backend.app.retrieval.service import RetrievalService


@dataclass(frozen=True)
class CandidateFilter:
    """Explicit, presentation-only filter set by the candidate list UI."""

    hard_rule: HardRuleOutcome | None = None
    channel: ChannelName | None = None


@dataclass(frozen=True)
class CandidateRankingRow:
    """One ranked candidate with display fields and per-channel results."""

    candidate_profile_id: UUID
    snapshot_order: int
    rrf_score: float
    display_name: str
    normalized_skills: list[str]
    years_experience: float | None
    education_level: str | None
    structured_rank: int | None
    structured_score: float | None
    keyword_rank: int | None
    keyword_score: float | None
    vector_rank: int | None
    vector_score: float | None
    hard_rule: HardRuleBundle | None


@dataclass(frozen=True)
class CandidateRankingView:
    """Frozen ranking view returned to the candidate list UI."""

    job_version_id: UUID
    config: RetrievalConfig
    rows: list[CandidateRankingRow]
    total: int  # full Top-K size, independent of any filter


def _row_passes(row: CandidateRankingRow, filters: CandidateFilter) -> bool:
    if filters.hard_rule is not None and (
        row.hard_rule is None or row.hard_rule.overall != filters.hard_rule
    ):
        return False
    if filters.channel is not None:
        if filters.channel is ChannelName.STRUCTURED and row.structured_rank is None:
            return False
        if filters.channel is ChannelName.KEYWORD and row.keyword_rank is None:
            return False
        if filters.channel is ChannelName.VECTOR and row.vector_rank is None:
            return False
    return True


async def preview_ranking(
    repository: RetrievalRepository,
    gateway: EmbeddingGateway,
    job_version_id: UUID,
    config: RetrievalConfig,
    filters: CandidateFilter,
    profile_views: dict[UUID, ReadyProfileView],
) -> CandidateRankingView:
    query = RetrievalQuery(
        job_version_id=job_version_id,
        top_k=config.top_k,
        rule_version=config.rule_version,
        channels=config.channels,
    )
    context = await RetrievalService(repository, gateway).run_recall_full(query)
    snapshot = build_ranking_snapshot(context.bundle, config, context.job, context.profiles)

    rows: list[CandidateRankingRow] = []
    for fused in snapshot.fused:
        view = profile_views.get(fused.candidate_profile_id)
        rows.append(
            CandidateRankingRow(
                candidate_profile_id=fused.candidate_profile_id,
                snapshot_order=fused.snapshot_order,
                rrf_score=fused.rrf_score,
                display_name=view.display_name if view is not None else "",
                normalized_skills=list(view.normalized_skills) if view is not None else [],
                years_experience=view.years_experience if view is not None else None,
                education_level=view.education_level if view is not None else None,
                structured_rank=fused.structured_rank,
                structured_score=fused.structured_score,
                keyword_rank=fused.keyword_rank,
                keyword_score=fused.keyword_score,
                vector_rank=fused.vector_rank,
                vector_score=fused.vector_score,
                hard_rule=fused.hard_rule,
            )
        )

    # `total` is the unfiltered Top-K size; filtering only trims the shown rows.
    total = len(rows)
    filtered = [row for row in rows if _row_passes(row, filters)]
    return CandidateRankingView(
        job_version_id=job_version_id,
        config=config,
        rows=filtered,
        total=total,
    )
