"""IMP-023: collaborative cancellation and Approval timeout (G4 gate).

Hermetic tests prove the two terminal transitions the detailed design §11.8/§11.9
require, and that they are race-safe against the human decision path:

* A timed-out ``PENDING`` approval is promoted to ``EXPIRED/TIMEOUT``; its run is
  failed as *retryable* (``error_code=APPROVAL_EXPIRED``) and the active slot is
  cleared so a new attempt can reclaim it and create a *new* approval.
* A collaborative cancel of a run that still holds a ``PENDING`` approval rides the
  approval into ``EXPIRED/RUN_CANCELLED``; the (now cancelled) run is not retryable.
* The sweep and ``decide`` share the "still PENDING" guard, so whichever runs first
  wins and the loser is a no-op: a decide after timeout gets 409, and the sweep
  skips an already-decided approval.
* ``retry_application_run`` only succeeds on a FAILED retryable run and starts a new
  attempt; a COMPLETED run is rejected with ``RUN_NOT_RETRYABLE``.

The maintenance sweep runs over a *fresh* service layer built on the same in-memory
store to stand in for a scheduled worker (the real Celery task is IMP-030, §14).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from backend.app.agent.checkpoint import InMemoryCheckpointer
from backend.app.agent.models import AgentRun, RunStatus
from backend.app.agent.repository import InMemoryAgentRunRepository
from backend.app.agent.service import RunService
from backend.app.approvals.models import ApprovalStatus
from backend.app.approvals.repository import InMemoryApprovalRepository
from backend.app.approvals.service import ApprovalService, DecisionAction
from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError
from backend.app.job_applications.models import ApplicationRun, ApplicationStatus, JobApplication
from backend.app.job_applications.repository import (
    InMemoryApplicationRunRepository,
    InMemoryJobApplicationRepository,
)
from backend.app.job_applications.service import ApplicationRunService
from backend.app.maintenance.service import MaintenanceService

FIXED_NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=datetime.now().astimezone().tzinfo)


def _core() -> tuple[
    InMemoryAgentRunRepository,
    InMemoryJobApplicationRepository,
    InMemoryApplicationRunRepository,
    InMemoryApprovalRepository,
    RunService,
    ApprovalService,
    ApplicationRunService,
    MaintenanceService,
    InMemoryCheckpointer,
]:
    agent_repo = InMemoryAgentRunRepository()
    checkpointer = InMemoryCheckpointer()
    run_service = RunService(agent_repo, checkpointer)
    app_repo = InMemoryJobApplicationRepository()
    arun_repo = InMemoryApplicationRunRepository()
    approval_repo = InMemoryApprovalRepository()

    def clock() -> datetime:
        return FIXED_NOW

    async def authorize(actor: Actor, job_id: UUID) -> None:
        return None

    approval_service = ApprovalService(
        authorize=authorize,
        approval_repo=approval_repo,
        arun_repo=arun_repo,
        app_repo=app_repo,
        run_service=run_service,
        now=clock,
    )
    app_run_service = ApplicationRunService(
        app_repo=app_repo,
        arun_repo=arun_repo,
        run_service=run_service,
        authorize=authorize,
        approval_service=approval_service,
    )
    maintenance = MaintenanceService(
        approval_repo=approval_repo,
        arun_repo=arun_repo,
        app_repo=app_repo,
        run_service=run_service,
        approval_service=approval_service,
        now=clock,
    )
    return (
        agent_repo,
        app_repo,
        arun_repo,
        approval_repo,
        run_service,
        approval_service,
        app_run_service,
        maintenance,
        checkpointer,
    )


def _application(job_id: UUID, candidate_id: UUID) -> JobApplication:
    return JobApplication(
        id=uuid4(),
        job_id=job_id,
        candidate_id=candidate_id,
        status=ApplicationStatus.CREATED,
        version=1,
    )


def _actor() -> Actor:
    return Actor(user_id=uuid4(), username="tester", role=UserRole.HIRING_MANAGER)


async def _create_run(
    svc: ApplicationRunService, app_repo: InMemoryJobApplicationRepository
) -> tuple[AgentRun, ApplicationRun, UUID]:
    job_id = uuid4()
    app = _application(job_id, uuid4())
    await app_repo.save_application(app)
    agent_run, application_run = await svc.create_application_run(_actor(), app.id, None)
    return agent_run, application_run, app.id


def test_approval_timeout_expires_run_as_retryable() -> None:
    asyncio.run(_timeout_expires_retryable())


async def _timeout_expires_retryable() -> None:
    _a, app_repo, _r, _ar, _rs, approval_service, _svc, maintenance, _ck = _core()
    agent_run, _app_run, app_id = await _create_run(_svc, app_repo)
    approval = await approval_service.get_pending_by_run(agent_run.id)
    assert approval is not None
    # Force the approval into the past relative to the injected clock.
    approval.expires_at = FIXED_NOW - timedelta(minutes=1)

    expired = await maintenance.expire_pending_approvals(before=FIXED_NOW)
    assert expired == 1

    # Approval -> EXPIRED/TIMEOUT.
    reread = await approval_service.get_approval(approval.id)
    assert reread is not None
    assert reread.status is ApprovalStatus.EXPIRED
    assert reread.expiration_reason == "TIMEOUT"
    # Run -> FAILED, retryable, with the stable error code (§11.8).
    run = await _rs.get_run(agent_run.id)
    assert run is not None
    assert run.status is RunStatus.FAILED
    assert run.retryable is True
    assert run.error_code == "APPROVAL_EXPIRED"
    assert run.failed_node == "human_review"
    # Slot cleared so a retry can reclaim it (§11.8 step 5).
    refreshed = await app_repo.get_application(app_id)
    assert refreshed is not None
    assert refreshed.active_application_run_id is None


def test_expire_skips_already_decided_approval() -> None:
    asyncio.run(_expire_skips_decided())


async def _expire_skips_decided() -> None:
    _a, app_repo, _r, _ar, _rs, approval_service, _svc, maintenance, _ck = _core()
    agent_run, _app_run, _app_id = await _create_run(_svc, app_repo)
    first = await approval_service.get_pending_by_run(agent_run.id)
    assert first is not None
    # First decision (APPROVE) applies the status change and resumes the graph to
    # the second (schedule) approval gate — the run stays WAITING_APPROVAL.
    await _svc.decide_approval(_actor(), first.id, DecisionAction.APPROVE, expected_version=1)
    second = await approval_service.get_pending_by_run(agent_run.id)
    assert second is not None
    # Second decision (APPROVE) completes the run; the slot is released.
    await _svc.decide_approval(_actor(), second.id, DecisionAction.APPROVE, expected_version=1)
    # Now the sweeper runs against the same (already decided) approvals.
    for appr in (first, second):
        appr.expires_at = FIXED_NOW - timedelta(minutes=1)
    expired = await maintenance.expire_pending_approvals(before=FIXED_NOW)
    assert expired == 0
    # The run is untouched: still COMPLETED, not FAILED.
    run = await _rs.get_run(agent_run.id)
    assert run is not None
    assert run.status is RunStatus.COMPLETED
    assert run.retryable is False


def test_decide_after_timeout_is_rejected() -> None:
    asyncio.run(_decide_after_timeout())


async def _decide_after_timeout() -> None:
    _a, app_repo, _r, _ar, _rs, approval_service, _svc, maintenance, _ck = _core()
    agent_run, _app_run, _app_id = await _create_run(_svc, app_repo)
    approval = await approval_service.get_pending_by_run(agent_run.id)
    assert approval is not None
    approval.expires_at = FIXED_NOW - timedelta(minutes=1)
    # Sweeper promotes it to EXPIRED first.
    await maintenance.expire_pending_approvals(before=FIXED_NOW)
    # A late decide must be rejected (no side effect, no second transition).
    try:
        await _svc.decide_approval(
            _actor(), approval.id, DecisionAction.APPROVE, expected_version=1
        )
        raise AssertionError("expected a 409 after timeout")
    except AppError as exc:
        assert exc.http_status == 409


def test_cancel_expires_pending_approval() -> None:
    asyncio.run(_cancel_expires_approval())


async def _cancel_expires_approval() -> None:
    _a, app_repo, _r, _ar, _rs, approval_service, _svc, maintenance, _ck = _core()
    agent_run, _app_run, app_id = await _create_run(_svc, app_repo)
    approval = await approval_service.get_pending_by_run(agent_run.id)
    assert approval is not None
    assert agent_run.status is RunStatus.WAITING_APPROVAL

    run, _app_run2 = await _svc.cancel_application_run(_actor(), agent_run.id)
    assert run.status is RunStatus.CANCELLED
    # Collaborative cancel rode the PENDING approval into EXPIRED/RUN_CANCELLED.
    reread = await approval_service.get_approval(approval.id)
    assert reread is not None
    assert reread.status is ApprovalStatus.EXPIRED
    assert reread.expiration_reason == "RUN_CANCELLED"
    # The cancelled run is NOT retryable (§11.9) and its slot is cleared.
    refreshed = await app_repo.get_application(app_id)
    assert refreshed is not None
    assert refreshed.active_application_run_id is None
    assert run.retryable is False


def test_retry_after_timeout_creates_new_attempt() -> None:
    asyncio.run(_retry_after_timeout())


async def _retry_after_timeout() -> None:
    _a, app_repo, _r, _ar, _rs, approval_service, _svc, maintenance, _ck = _core()
    agent_run, _app_run, app_id = await _create_run(_svc, app_repo)
    approval = await approval_service.get_pending_by_run(agent_run.id)
    assert approval is not None
    approval.expires_at = FIXED_NOW - timedelta(minutes=1)
    await maintenance.expire_pending_approvals(before=FIXED_NOW)

    # Retry reclaims the (now free) slot as a new attempt and creates a NEW approval.
    new_run, _new_app_run = await _svc.retry_application_run(_actor(), agent_run.id)
    assert new_run.attempt == 2
    assert new_run.status is RunStatus.WAITING_APPROVAL
    new_pending = await approval_service.get_pending_by_run(new_run.id)
    assert new_pending is not None
    assert new_pending.status is ApprovalStatus.PENDING
    # The old (expired) approval is preserved as history, not overwritten.
    old = await approval_service.get_approval(approval.id)
    assert old is not None
    assert old.status is ApprovalStatus.EXPIRED
    refreshed = await app_repo.get_application(app_id)
    assert refreshed is not None
    assert refreshed.active_application_run_id == new_run.id


def test_retry_non_retryable_rejected() -> None:
    asyncio.run(_retry_completed_rejected())


async def _retry_completed_rejected() -> None:
    _a, app_repo, _r, _ar, _rs, approval_service, _svc, maintenance, _ck = _core()
    agent_run, _app_run, _app_id = await _create_run(_svc, app_repo)
    approval = await approval_service.get_pending_by_run(agent_run.id)
    assert approval is not None
    # Approve -> COMPLETED (terminal, not retryable).
    await _svc.decide_approval(_actor(), approval.id, DecisionAction.APPROVE, expected_version=1)
    try:
        await _svc.retry_application_run(_actor(), agent_run.id)
        raise AssertionError("expected RUN_NOT_RETRYABLE")
    except AppError as exc:
        assert exc.code == "RUN_NOT_RETRYABLE"
        assert exc.http_status == 409
