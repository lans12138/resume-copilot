"""RankingsProvider adapters for the MatchRun API (IMP-026).

``MatchRunService`` consumes a ``RankingsProvider`` that returns a frozen
``RankingSnapshot`` for a job version. Tests inject ``FakeRankingsProvider`` with a
canned snapshot; the real API uses ``SqlRankingsProvider``, which drives the
existing retrieval stack (three-channel recall + RRF fusion + hard rules) so the
snapshot is fully reproducible from live projections (detailed design §10.1).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.settings import Settings
from backend.app.infrastructure.embedding import build_embedding_gateway
from backend.app.retrieval.models import (
    RankingSnapshot,
    RetrievalConfig,
    RetrievalQuery,
)
from backend.app.retrieval.ranking import build_ranking_snapshot
from backend.app.retrieval.repository import SqlRetrievalRepository
from backend.app.retrieval.service import RetrievalService


class SqlRankingsProvider:
    """Produce a RankingSnapshot from the live retrieval stack (PostgreSQL)."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._config = RetrievalConfig.from_settings(settings)
        self._service = RetrievalService(
            SqlRetrievalRepository(session), build_embedding_gateway(settings)
        )

    async def get_snapshot(self, *, job_version_id: UUID) -> RankingSnapshot:
        context = await self._service.run_recall_full(
            RetrievalQuery(
                job_version_id=job_version_id,
                top_k=self._config.top_k,
                rule_version=self._config.rule_version,
                channels=self._config.channels,
            )
        )
        return build_ranking_snapshot(
            context.bundle, self._config, context.job, context.profiles
        )


class FakeRankingsProvider:
    """Return a fixed snapshot; used by hermetic route/integration tests."""

    def __init__(self, snapshot: RankingSnapshot) -> None:
        self._snapshot = snapshot

    async def get_snapshot(self, *, job_version_id: UUID) -> RankingSnapshot:
        return self._snapshot
