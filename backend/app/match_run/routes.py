"""MatchRun API endpoints (IMP-026 / FIN-005, detailed design §12.4).

``POST /jobs/{id}/match-runs`` records the run header, authorizes on the job, and
hands execution to a worker: the request answers 202 while the batch analysis
(§10.2, no side effects, no approval) runs off the request path. Progress is
followed live via ``GET /match-runs/{id}/events`` (IMP-025) and polled through
``GET /match-runs/{id}`` until the run reaches a terminal status.

The route builds all adapters from one session and reuses the same job-level
authorization check as everywhere else. Publication happens *after* the business
facts are committed, and its failure never fails the request (§14.4).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from backend.app.agent.checkpoint import SqlCheckpointer
from backend.app.agent.enqueuer import CeleryRunEnqueuer, ExecutionIntent
from backend.app.agent.models import AgentRun, RunStatus, RunType
from backend.app.agent.repository import SqlAgentRunRepository
from backend.app.agent.service import RunService
from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.core.errors import app_error
from backend.app.idempotency.dependency import IdempotencyGuardDep
from backend.app.infrastructure.celery import app as celery_app
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.jobs.service import JobService
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
from backend.app.match_run.wiring import build_match_run_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["match-runs"])

ActorDep = Annotated[Actor, Depends(get_current_actor)]


def _publish_match_run(run: AgentRun, *, intent: ExecutionIntent = ExecutionIntent.START) -> None:
    """Deliver the execution task after the run is committed (§14.4).

    A broker failure must not fail the request: the run is already durable, and
    ``CREATED`` means both "enqueued" and "not yet delivered", so the republish
    scan (FIN-006) recovers it without any extra flag. Raising here would turn a
    recoverable Redis outage into a 5xx for a run that unambiguously exists.

    ``intent`` is the one thing that makes a retry real: ``FAILED`` looks exactly
    like a redelivery of the first pass to the worker, so only the *published*
    intent lets it tell "retry, please" from "duplicated message, ignore". It is
    carried as a task argument by ``enqueue_match_run`` and read back by the
    claim, never inferred from the stored status.
    """
    try:
        CeleryRunEnqueuer(celery_app).enqueue_match_run(
            run.id, attempt=run.attempt, intent=intent
        )
    except Exception:  # noqa: BLE001 - a Redis outage must not fail a committed run
        logger.warning("match_run.publish_failed run_id=%s", run.id, exc_info=True)


@router.post("/jobs/{job_id}/match-runs", status_code=202, response_model=MatchRunAccepted)
async def create_match_run(
    job_id: UUID,
    payload: CreateMatchRunRequest,
    actor: ActorDep,
    request: Request,
) -> MatchRunAccepted:
    """Create a job-level MatchRun and enqueue its execution (no approval)."""
    resources: RuntimeResources = request.app.state.resources
    settings = request.app.state.settings
    async with resources.session_factory() as session:
        job = await JobService(session).get_authorized(actor, job_id)
        if job.current_version_id is None:
            raise app_error("JOB_NO_VERSION", http_status=409, safe_message="岗位尚无可用版本")
        service = build_match_run_service(session, settings)
        run, _match_run = await service.create_match_run(
            job_id=job.id,
            job_version_id=job.current_version_id,
            actor_id=actor.user_id,
            retrieval_config=payload.retrieval_config or {},
            model_config=payload.model_config_override or {},
            prompt_version=payload.prompt_version,
            rule_version=payload.rule_version,
        )
        # Commit the business fact first: a run that exists and was never published
        # is recoverable, a publication for a run that was rolled back is not.
        await session.commit()
        _publish_match_run(run)
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
async def retry_match_run(
    run_id: UUID, actor: ActorDep, request: Request, guard: IdempotencyGuardDep
) -> MatchRunAccepted:
    """Re-drive a FAILED, retryable MatchRun as a new attempt (§5.6).

    The run keeps its row (and its ``thread_id``); ``attempt`` advances so the new
    execution slice is distinguishable, and the status stays ``FAILED`` until a
    worker claims it with the *retry* intent. Leaving it ``FAILED`` is what makes
    the retry explicit: nothing about the stored state invites execution, only the
    published intent does.
    """
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        agent_repo = SqlAgentRunRepository(session)
        agent_run = await agent_repo.get_run(run_id)
        if agent_run is None or agent_run.run_type is not RunType.MATCH:
            raise app_error("MATCH_RUN_NOT_FOUND", http_status=404, safe_message="分析流程不存在")
        await _authorize_from_run(actor, agent_run, session)
        if agent_run.status is not RunStatus.FAILED or not agent_run.retryable:
            raise app_error(
                "RUN_NOT_RETRYABLE",
                http_status=409,
                safe_message="该分析流程不可重试",
                details={"status": agent_run.status.value, "retryable": agent_run.retryable},
            )
        match_run = await SqlMatchRunRepository(session).get_match_run(run_id)
        if match_run is None:
            raise app_error("MATCH_RUN_NOT_FOUND", http_status=404, safe_message="分析流程不存在")
        agent_run.attempt += 1
        await session.commit()
        _publish_match_run(agent_run, intent=ExecutionIntent.RETRY)
        result = MatchRunAccepted(
            run_id=agent_run.id, job_id=match_run.job_id, status=agent_run.status.value
        )
        await guard.complete(202, result.model_dump(mode="json"), resource_id=str(run_id))
        return result


@router.post("/match-runs/{run_id}/cancel", response_model=MatchRunAccepted)
async def cancel_match_run(
    run_id: UUID, actor: ActorDep, request: Request, guard: IdempotencyGuardDep
) -> MatchRunAccepted:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        agent_repo = SqlAgentRunRepository(session)
        agent_run = await agent_repo.get_run(run_id)
        if agent_run is None or agent_run.run_type is not RunType.MATCH:
            raise app_error("MATCH_RUN_NOT_FOUND", http_status=404, safe_message="分析流程不存在")
        await _authorize_from_run(actor, agent_run, session)
        # Session-scoped checkpointer: the same store the run was written with. A
        # worker delivery that arrives after this sees a cancellation marker and
        # refuses to start (agent.tasks.decide_claim), so the flag — not the
        # in-process adapter — is what actually stops the run.
        run_service = RunService(
            agent_repo, SqlCheckpointer(session), notifier=resources.event_notifier
        )
        await run_service.cancel_run(agent_run, reason="user_cancel")
        match_run = await SqlMatchRunRepository(session).get_match_run(run_id)
        job_id = match_run.job_id if match_run is not None else run_id
        await session.commit()
        result = MatchRunAccepted(run_id=agent_run.id, job_id=job_id, status=agent_run.status.value)
        await guard.complete(202, result.model_dump(mode="json"), resource_id=str(run_id))
        return result


async def _authorize_from_run(actor: Actor, agent_run: Any, session: Any) -> None:
    """Resolve the job_id from the run snapshot and re-run job-level auth."""
    job_id_raw = (agent_run.config_snapshot_json or {}).get("job_id")
    if job_id_raw is None:
        raise app_error("MATCH_RUN_NOT_FOUND", http_status=404, safe_message="分析流程无岗位上下文")
    await JobService(session).get_authorized(actor, UUID(str(job_id_raw)))
