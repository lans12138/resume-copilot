"""Candidate ranking preview endpoint (IMP-017).

GET /api/v1/jobs/{job_id}/candidates runs the same recall + fusion + hard rules
used by a MatchRun (read-only, no MatchRun side effects) and returns the ranked
candidate list with per-channel results and hard-rule verdicts. The optional
``hard_rule``/``channel`` query params apply a *presentation-only* filter; the
returned ``total`` always reflects the full Top-K so the UI filter can never
shrink the MatchRun scope.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.candidates.repository import SqlCandidateProfileRepository
from backend.app.candidates.schemas import CandidateListResponse
from backend.app.infrastructure.embedding import build_embedding_gateway
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.jobs.service import JobService
from backend.app.retrieval.models import ChannelName, HardRuleOutcome, RetrievalConfig
from backend.app.retrieval.preview import CandidateFilter, preview_ranking
from backend.app.retrieval.repository import SqlRetrievalRepository

router = APIRouter(prefix="/api/v1/jobs", tags=["candidates"])


@router.get("/{job_id}/candidates", response_model=CandidateListResponse)
async def list_job_candidates(
    job_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
    hard_rule: Annotated[HardRuleOutcome | None, Query()] = None,
    channel: Annotated[ChannelName | None, Query()] = None,
) -> CandidateListResponse:
    resources: RuntimeResources = request.app.state.resources
    settings = request.app.state.settings
    async with resources.session_factory() as session:
        job_service = JobService(session)
        job = await job_service.get_authorized(actor, job_id)
        version = await job_service.current_version(job)
        retrieval_repo = SqlRetrievalRepository(session)
        profile_repo = SqlCandidateProfileRepository(session)
        views = {view.profile_id: view for view in await profile_repo.list_ready_views()}
        gateway = build_embedding_gateway(settings)
        config = RetrievalConfig.from_settings(settings)
        preview = await preview_ranking(
            retrieval_repo,
            gateway,
            version.id,
            config,
            CandidateFilter(hard_rule=hard_rule, channel=channel),
            views,
        )
        return CandidateListResponse.from_view(preview, job_id)
