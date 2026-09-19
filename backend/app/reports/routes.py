"""MatchRun report query endpoint (IMP-020).

GET /api/v1/match-runs/{id}/reports returns every evidence-backed report a
MatchRun persisted, gated by the same resource-level ``JobAssignment``
authorization every job-scoped read uses. This is the "report is queryable"
half of the IMP-020 gate.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.candidates.repository import SqlEvidenceChunkRepository
from backend.app.candidates.summaries import load_display_summaries
from backend.app.core.errors import AppError
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.jobs.service import JobService
from backend.app.match_run.repository import SqlMatchRunRepository
from backend.app.reports.repository import SqlReportRepository
from backend.app.reports.schemas import ReportList

router = APIRouter(prefix="/api/v1/match-runs", tags=["reports"])


@router.get("/{run_id}/reports", response_model=ReportList)
async def list_run_reports(
    run_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> ReportList:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        job_service = JobService(session)
        match_run_repo = SqlMatchRunRepository(session)
        report_repo = SqlReportRepository(session)

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

        views = await report_repo.list_by_run(run_id)
        # PORT-005: name each report's candidate and hand the client the source
        # document, so the report panel can be read by name and its evidence can be
        # opened at the original text instead of by hunting through the documents list.
        summaries = await load_display_summaries(
            session, [v.report.candidate_profile_id for v in views]
        )
        locators = await SqlEvidenceChunkRepository(session).list_locators(
            [e.evidence_chunk_id for v in views for c in v.claims for e in c.evidences]
        )
        return ReportList.from_views(views, summaries, locators)
