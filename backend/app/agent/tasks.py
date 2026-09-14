"""Celery agent task: execute a MatchRun off the request path (FIN-005, §14).

The API's job is to record *business facts* — the run header, the frozen
candidate snapshot, the active-run slot — and answer 202. A worker then owns the
expensive part. This module is that worker's entry point for the job-level batch
analysis; ``agent.execute_application_run`` lands together with the ApplicationRun
route split, because a worker must never continue a run that is paused for a human
decision (see the note on :class:`ExecutionIntent`).

Why a task is a *claim*, not a fire-and-forget call: Celery is at-least-once, so the
same slice of work can be delivered twice, and a run must never be executed twice
(duplicate work) nor be moved backwards out of a terminal state (FIN-005's "no
terminal-state regression"). Every entry point therefore starts by locking the run
row and asking :func:`decide_claim` what to do. The lock is held for the whole
execution — the batch graph runs inside the claim's transaction — so a duplicate
delivery blocks on the row and, once the first slice commits, correctly observes a
terminal status instead of racing it.

That transaction boundary is also the crash story: because the whole pass commits
once, a worker killed mid-run leaves the row exactly as it was created, so
``maintenance.republish_queued`` (FIN-006) can safely re-deliver it. A partially
completed pass is never visible to any other reader.

Async note (same constraint as ``candidates.tasks``): each invocation builds its own
``RuntimeResources`` and runs inside a *single* ``asyncio.run``. The async engine and
the redis client are loop-bound, and a prefork worker forks after module import, so
sharing a cached ``RuntimeResources`` — or splitting the work across several event
loops — raises "attached to a different loop". Resources are disposed in the same
loop that built them.
"""

from __future__ import annotations

import asyncio
import logging
import random
from enum import StrEnum
from typing import Any
from uuid import UUID

from celery import Task, shared_task  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.models import AgentRun, RunStatus, RunType, is_terminal
from backend.app.candidates.repository import SqlEvidenceChunkRepository
from backend.app.core.settings import get_settings
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.match_run.repository import SqlMatchRunRepository
from backend.app.match_run.wiring import build_match_run_service
from backend.app.reports.repository import SqlReportRepository
from backend.app.reports.service import ReportService

logger = logging.getLogger(__name__)


class ExecutionIntent(StrEnum):
    """What the caller wants to do with the run it is claiming.

    ``START`` drives a run that has been enqueued but has not begun; ``RETRY``
    re-drives one that already failed. Both are explicit because they are the only
    two ways a run may *enter* execution, and the distinction is what keeps
    ``WAITING_APPROVAL`` untouchable from an ordinary delivery: a run parked at its
    human gate accepts neither.
    """

    START = "start"
    RETRY = "retry"


class ClaimDecision(StrEnum):
    """Outcome of the claim handshake, decided before any work is done."""

    CLAIMED = "claimed"
    NOT_FOUND = "not_found"
    ALREADY_TERMINAL = "already_terminal"
    CANCELLED_BEFORE_START = "cancelled_before_start"
    PAUSED_FOR_APPROVAL = "paused_for_approval"
    ALREADY_RUNNING = "already_running"
    NOT_STARTABLE = "not_startable"
    NOT_RETRYABLE = "not_retryable"


def decide_claim(
    *,
    status: RunStatus | None,
    cancel_requested: bool,
    intent: ExecutionIntent,
) -> ClaimDecision:
    """Decide whether this delivery may execute the run.

    Pure on purpose: the whole at-least-once policy is readable and testable here,
    instead of being spread across the worker and the API.
    """
    if status is None:
        return ClaimDecision.NOT_FOUND
    if cancel_requested:
        # A user asked for this run to stop. Honouring the request outranks every
        # other reason to execute, including a retry.
        return ClaimDecision.CANCELLED_BEFORE_START
    if intent is ExecutionIntent.RETRY:
        # §5.6: a FAILED run is retryable by definition of this intent; anything
        # else (CREATED, RUNNING, WAITING_APPROVAL, or another terminal state)
        # must not be re-driven through the retry door.
        if status is RunStatus.FAILED:
            return ClaimDecision.CLAIMED
        return ClaimDecision.NOT_RETRYABLE
    if is_terminal(status):
        # An at-least-once redelivery of a finished run, or a first-pass task that
        # raced a user cancellation. Never move a terminal run.
        return ClaimDecision.ALREADY_TERMINAL
    if status is RunStatus.CREATED:
        return ClaimDecision.CLAIMED
    if status is RunStatus.WAITING_APPROVAL:
        # Someone already drove this run to its human gate. Restarting it here is
        # exactly the bypass the approval exists to prevent (§11.2, "未审批副作用
        # 执行次数必须为 0").
        return ClaimDecision.PAUSED_FOR_APPROVAL
    if status is RunStatus.RUNNING:
        # The row lock means we are not racing a live claim: either the previous
        # worker died holding it, or the status was set outside the claim. Telling
        # those apart needs a lease/heartbeat, which is FIN-006's republish scan — a
        # bare status cannot, and guessing would risk a second pass over a run that
        # is still making progress.
        return ClaimDecision.ALREADY_RUNNING
    return ClaimDecision.NOT_STARTABLE


async def claim_run(
    session: AsyncSession, run_id: UUID, *, intent: ExecutionIntent
) -> tuple[AgentRun | None, ClaimDecision]:
    """Lock the run row and decide whether this delivery may execute it."""
    # ``with_for_update`` is what makes the decision above safe: a second delivery
    # waits here until the first commits, so it cannot observe a stale status.
    run = await session.get(AgentRun, run_id, with_for_update=True)
    if run is None:
        return None, ClaimDecision.NOT_FOUND
    decision = decide_claim(
        status=run.status,
        cancel_requested=run.cancel_requested_at is not None,
        intent=intent,
    )
    return run, decision


async def _execute_match_run_async(resources: RuntimeResources, run_id: UUID) -> dict[str, Any]:
    session = resources.session_factory()
    try:
        run, decision = await claim_run(session, run_id, intent=ExecutionIntent.START)
        if run is None or decision is not ClaimDecision.CLAIMED:
            await session.rollback()
            logger.info(
                "agent.execute_match_run skipped run_id=%s reason=%s", run_id, decision.value
            )
            return {"status": "skipped", "reason": decision.value, "run_id": str(run_id)}
        assert run.run_type is RunType.MATCH

        match_run = await SqlMatchRunRepository(session).get_match_run(run_id)
        if match_run is None:
            await session.rollback()
            return {"status": "skipped", "reason": "match_run_not_found", "run_id": str(run_id)}

        service = build_match_run_service(session, get_settings())
        await service.execute_match_run(
            run=run,
            match_run=match_run,
            report_service=ReportService(SqlReportRepository(session)),
            evidence_provider=SqlEvidenceChunkRepository(session),
        )
        # One commit for the whole pass: the claim's row lock is released only when
        # the run has reached its terminal state, so a duplicate delivery can never
        # observe a half-finished run, and a crash rolls back to CREATED.
        await session.commit()
        return {"status": "ok", "run_id": str(run_id), "final_status": run.status.value}
    finally:
        await session.close()
        # Tear down the loop-bound engine/redis inside the same event loop so no
        # loop-bound connection survives into the next task's loop.
        await resources.close()


def _execute_match_run(run_id: str) -> dict[str, Any]:
    resources = RuntimeResources.build(get_settings())
    return asyncio.run(_execute_match_run_async(resources, UUID(run_id)))


@shared_task(name="agent.execute_match_run", bind=True)  # type: ignore[untyped-decorator]
def execute_match_run(self: Task, run_id: str) -> dict[str, Any]:
    """Drive a job-level MatchRun to its terminal state.

    Retries follow §14.2: transient failures back off exponentially with jitter
    and give up after the configured budget. Exhausting the budget does *not*
    invent a terminal status — the run stays CREATED/FAILED as the database has
    it, and the caller's next delivery (or FIN-006's republish scan) decides.
    """
    try:
        return _execute_match_run(run_id)
    except Exception as error:  # noqa: BLE001 - Celery retry policy boundary
        settings = get_settings()
        attempt_no = self.request.retries
        maximum = settings.max_transient_retries
        if attempt_no >= maximum:
            logger.exception(
                "agent.execute_match_run exhausted retries run_id=%s", run_id
            )
            return {"status": "transient_exhausted", "run_id": run_id}
        countdown = min(2**attempt_no, 30) + random.uniform(0, 2)
        raise self.retry(exc=error, countdown=countdown, max_retries=maximum) from error
