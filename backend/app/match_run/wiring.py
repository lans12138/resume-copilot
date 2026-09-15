"""Service assembly shared by the MatchRun route and the agent worker (FIN-005).

The API creates a MatchRun and a worker executes it, but both must build the
*batch-analysis service* identically: same repositories, same rankings source,
same concurrency ceiling. Keeping one builder here means the worker cannot drift
from the request path — a drift that would be invisible until a run produced a
different ranking than the API preview promised.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.repository import SqlAgentRunRepository
from backend.app.core.settings import Settings
from backend.app.match_run.applications import SqlApplicationsProvider
from backend.app.match_run.rankings import SqlRankingsProvider
from backend.app.match_run.repository import (
    SqlMatchRunCandidateRepository,
    SqlMatchRunRepository,
)
from backend.app.match_run.service import MatchRunService
from backend.app.sse.notifier import EventNotifier


def build_match_run_service(
    session: AsyncSession,
    settings: Settings,
    notifier: EventNotifier | None = None,
) -> MatchRunService:
    """Assemble the MatchRun service + repositories over one transaction."""
    return MatchRunService(
        run_repository=SqlAgentRunRepository(session),
        match_run_repository=SqlMatchRunRepository(session),
        candidate_repository=SqlMatchRunCandidateRepository(session),
        rankings=SqlRankingsProvider(session, settings),
        applications=SqlApplicationsProvider(session),
        # AsyncSession cannot be flushed concurrently. The service keeps its
        # bounded fan-out seam for worker-scoped repositories, while one
        # transaction processes candidates one at a time.
        notifier=notifier,
        concurrency=1,
    )
