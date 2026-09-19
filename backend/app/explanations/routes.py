"""MatchRun explanation query endpoint (PORT-003).

GET /api/v1/match-runs/{id}/explanations returns the model call record for every
candidate the run explained, gated by the same resource-level ``JobAssignment``
authorization every job-scoped read uses.

The endpoint exists because an unavailable explanation is a state a reader has to
be able to see. Without it, a report with no model claims is indistinguishable
from a report whose model was rate limited — the exact ambiguity BR-010 forbids,
and the one that decides whether an operator should re-run or investigate.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError
from backend.app.explanations.repository import SqlExplanationRepository
from backend.app.explanations.schemas import ExplanationList
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.jobs.service import JobService
from backend.app.match_run.repository import SqlMatchRunRepository

router = APIRouter(prefix="/api/v1/match-runs", tags=["explanations"])


@router.get("/{run_id}/explanations", response_model=ExplanationList)
async def list_run_explanations(
    run_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> ExplanationList:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        job_service = JobService(session)
        match_run_repo = SqlMatchRunRepository(session)
        explanation_repo = SqlExplanationRepository(session)

        match_run = await match_run_repo.get_match_run(run_id)
        if match_run is None:
            raise AppError(
                code="MATCH_RUN_NOT_FOUND",
                http_status=404,
                safe_message="未找到对应的 MatchRun",
                details={"run_id": str(run_id)},
            )
        # Resource-level authorization: the actor must be assigned to the job.
        await job_service.get_authorized(actor, match_run.job_id)

        rows = await explanation_repo.list_by_run(run_id)
        return ExplanationList.from_rows(rows)
