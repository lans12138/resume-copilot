"""MatchRun API endpoints (IMP-026, detailed design §12.4).

``POST /jobs/{id}/match-runs`` creates the run header, authorizes on the job, and
executes the batch-analysis graph to a terminal state (no side effects, no
approval). Progress is followed live via ``GET /match-runs/{id}/events`` (IMP-025).
The route builds all adapters from one session; authorization reuses the same
job-level check used everywhere else.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from backend.app.agent.models import RunStatus, RunType
from backend.app.agent.repository import SqlAgentRunRepository
from backend.app.agent.service import RunService
from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.core.errors import app_error
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.jobs.service import JobService
from backend.app.match_run.rankings import SqlRankingsProvider
from backend.app.match_run.repository import (
    SqlMatchRunCandidateRepository,
    SqlMatchRunRepository,
)
from backend.app.match_run.schemas import (
    CreateMatchRunRequest,
    MatchRunAccepted,
    MatchRunCandidateOut,
    MatchRunDetail,
    MatchRunList,
    MatchRunSummary,
)
from backend.app.match_run.service import MatchRunService

router = APIRouter(prefix="/api/v1", tags=["match-runs"])


def _build(session: Any, resources: RuntimeResources, settings: Any) -> MatchRunService:
    """Assemble the MatchRun service + repositories from one session."""
    agent_repo = SqlAgentRunRepository(session)
    rankings = SqlRankingsProvider(session, settings)
    return MatchRunService(
        run_repository=agent_repo,
        match_run_repository=SqlMatchRunRepository(session),
        candidate_repository=SqlMatchRunCandidateRepository(session),
        rankings=rankings,
    )


ActorDep = Annotated[Actor, Depends(get_current_actor)]


@router.post("/jobs/{job_id}/match-runs", status_code=202, response_model=MatchRunAccepted)
async def create_match_run(
    job_id: UUID,
    payload: CreateMatchRunRequest,
    actor: ActorDep,
    request: Request,
) -> MatchRunAccepted:
    """Create and execute a job-level MatchRun (batch analysis, no approval)."""
    resources: RuntimeResources = request.app.state.resources
    settings = request.app.state.settings
    async with resources.session_factory() as session:
        job_service = JobService(session)
        job = await job_service.get_authorized(actor, job_id)
        if job.current_version_id is None:
            raise app_error("JOB_NO_VERSION", http_status=409, safe_message="岗位尚无可用版本")
        service = _build(session, resources, settings)
        run, match_run = await service.create_match_run(
            job_id=job.id,
            job_version_id=job.current_version_id,
            actor_id=actor.user_id,
            retrieval_config=payload.retrieval_config or {},
            model_config=payload.model_config_override or {},
            prompt_version=payload.prompt_version,
            rule_version=payload.rule_version,
            application_ids={},
        )
        await service.execute_match_run(run=run, match_run=match_run, application_ids={})
        return MatchRunAccepted(run_id=run.id, job_id=job.id, status=run.status.value)


@router.get("/jobs/{job_id}/match-runs", response_model=MatchRunList)
async def list_match_runs(job_id: UUID, actor: ActorDep, request: Request) -> MatchRunList:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        job_service = JobService(session)
        await job_service.get_authorized(actor, job_id)
        match_run_repo = SqlMatchRunRepository(session)
        agent_repo = SqlAgentRunRepository(session)
        runs = await match_run_repo.list_by_job(job_id)
        summaries: list[MatchRunSummary] = []
        for match_run in runs:
            agent_run = await agent_repo.get_run(match_run.run_id)
            summaries.append(
                MatchRunSummary(
                    run_id=match_run.run_id,
                    job_id=match_run.job_id,
                    job_version_id=match_run.job_version_id,
                    status=agent_run.status.value if agent_run is not None else "UNKNOWN",
                    attempt=agent_run.attempt if agent_run is not None else 1,
                    created_at=match_run.created_at,
                )
            )
        return MatchRunList(runs=summaries)


@router.get("/match-runs/{run_id}", response_model=MatchRunDetail)
async def get_match_run(run_id: UUID, actor: ActorDep, request: Request) -> MatchRunDetail:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        agent_repo = SqlAgentRunRepository(session)
        agent_run = await agent_repo.get_run(run_id)
        if agent_run is None or agent_run.run_type is not RunType.MATCH:
            raise app_error("MATCH_RUN_NOT_FOUND", http_status=404, safe_message="分析流程不存在")
        await _authorize_from_run(actor, agent_run, session)
        match_run_repo = SqlMatchRunRepository(session)
        candidate_repo = SqlMatchRunCandidateRepository(session)
        match_run = await match_run_repo.get_match_run(run_id)
        if match_run is None:
            raise app_error("MATCH_RUN_NOT_FOUND", http_status=404, safe_message="分析流程不存在")
        candidates = await candidate_repo.get_candidates(run_id)
        return MatchRunDetail(
            run_id=match_run.run_id,
            job_id=match_run.job_id,
            job_version_id=match_run.job_version_id,
            status=agent_run.status.value,
            attempt=agent_run.attempt,
            rule_version=match_run.rule_version,
            prompt_version=match_run.prompt_version,
            created_at=match_run.created_at,
            finished_at=agent_run.finished_at,
            candidates=[
                MatchRunCandidateOut(
                    candidate_profile_id=c.candidate_profile_id,
                    application_id=c.application_id,
                    snapshot_order=c.snapshot_order,
                    rrf_score=float(c.rrf_score),
                    processing_status=c.processing_status.value,
                    hard_rule_overall=(
                        str((c.hard_rule_result_json or {}).get("overall"))
                        if c.hard_rule_result_json is not None
                        else None
                    ),
                )
                for c in candidates
            ],
        )


@router.post("/match-runs/{run_id}/retry", response_model=MatchRunAccepted)
async def retry_match_run(run_id: UUID, actor: ActorDep, request: Request) -> MatchRunAccepted:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        agent_repo = SqlAgentRunRepository(session)
        agent_run = await agent_repo.get_run(run_id)
        if agent_run is None or agent_run.run_type is not RunType.MATCH:
            raise app_error("MATCH_RUN_NOT_FOUND", http_status=404, safe_message="分析流程不存在")
        await _authorize_from_run(actor, agent_run, session)
        if agent_run.status != RunStatus.FAILED:
            raise app_error(
                "RUN_NOT_RETRYABLE",
                http_status=409,
                safe_message="该分析流程不可重试",
                details={"status": agent_run.status.value},
            )
        match_run = await SqlMatchRunRepository(session).get_match_run(run_id)
        if match_run is None:
            raise app_error("MATCH_RUN_NOT_FOUND", http_status=404, safe_message="分析流程不存在")
        service = _build(session, resources, request.app.state.settings)
        await service.execute_match_run(run=agent_run, match_run=match_run, application_ids={})
        return MatchRunAccepted(
            run_id=agent_run.id, job_id=match_run.job_id, status=agent_run.status.value
        )


@router.post("/match-runs/{run_id}/cancel", response_model=MatchRunAccepted)
async def cancel_match_run(run_id: UUID, actor: ActorDep, request: Request) -> MatchRunAccepted:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        agent_repo = SqlAgentRunRepository(session)
        agent_run = await agent_repo.get_run(run_id)
        if agent_run is None or agent_run.run_type is not RunType.MATCH:
            raise app_error("MATCH_RUN_NOT_FOUND", http_status=404, safe_message="分析流程不存在")
        await _authorize_from_run(actor, agent_run, session)
        run_service = RunService(
            agent_repo, resources.checkpointer, notifier=resources.event_notifier
        )
        await run_service.cancel_run(agent_run, reason="user_cancel")
        match_run = await SqlMatchRunRepository(session).get_match_run(run_id)
        job_id = match_run.job_id if match_run is not None else run_id
        return MatchRunAccepted(run_id=agent_run.id, job_id=job_id, status=agent_run.status.value)


async def _authorize_from_run(actor: Actor, agent_run: Any, session: Any) -> None:
    """Resolve the job_id from the run snapshot and re-run job-level auth."""
    job_id_raw = (agent_run.config_snapshot_json or {}).get("job_id")
    if job_id_raw is None:
        raise app_error("MATCH_RUN_NOT_FOUND", http_status=404, safe_message="分析流程无岗位上下文")
    await JobService(session).get_authorized(actor, UUID(str(job_id_raw)))
