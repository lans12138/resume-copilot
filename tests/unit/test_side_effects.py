"""IMP-024: side-effect execution service (exactly once + failure recovery).

These tests prove the gate detailed design §11.6/§11.7 and G5 (line 317) require:
the approved mutation is applied exactly once, gated by the approval idempotency
key; illegal parameters are rejected; and a retryable schedule-backend failure
moves the approval to EXECUTION_FAILED and the run to retryable-FAILED, after
which the *same* idempotency key can be retried to a successful EXECUTED state.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from backend.app.agent.checkpoint import InMemoryCheckpointer
from backend.app.agent.models import AgentRun, RunStatus
from backend.app.agent.repository import InMemoryAgentRunRepository
from backend.app.agent.service import RunService
from backend.app.approvals.models import ApprovalActionType, ApprovalStatus
from backend.app.approvals.repository import InMemoryApprovalRepository
from backend.app.approvals.service import ApprovalService, DecisionAction
from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError
from backend.app.interviews.repository import InMemoryInterviewRepository
from backend.app.interviews.schedule import MockScheduleBackend
from backend.app.interviews.schemas import (
    ScheduleProposal,
    ScheduleResult,
    ScheduleStatus,
)
from backend.app.job_applications.models import ApplicationRun, ApplicationStatus, JobApplication
from backend.app.job_applications.repository import (
    InMemoryApplicationRunRepository,
    InMemoryJobApplicationRepository,
)
from backend.app.job_applications.service import ApplicationRunService
from backend.app.job_applications.side_effects import (
    ApplicationSideEffectService,
    _RetryableScheduleError,
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


async def _always_authorize(actor: Actor, job_id: UUID) -> None:
    return None


def _core_with_side_effects(
    schedule_backend: Any = None,
) -> tuple[
    InMemoryJobApplicationRepository,
    InMemoryApplicationRunRepository,
    RunService,
    InMemoryApprovalRepository,
    ApprovalService,
    ApplicationSideEffectService,
    ApplicationRunService,
    InMemoryInterviewRepository,
]:
    agent_repo = InMemoryAgentRunRepository()
    checkpointer = InMemoryCheckpointer()
    run_service = RunService(agent_repo, checkpointer)
    app_repo = InMemoryJobApplicationRepository()
    arun_repo = InMemoryApplicationRunRepository()
    approval_repo = InMemoryApprovalRepository()
    interview_repo = InMemoryInterviewRepository()
    backend = schedule_backend or MockScheduleBackend()

    approval_service = ApprovalService(
        authorize=_always_authorize,
        approval_repo=approval_repo,
        arun_repo=arun_repo,
        app_repo=app_repo,
        run_service=run_service,
    )
    side_effects = ApplicationSideEffectService(
        approval_service=approval_service,
        app_repo=app_repo,
        arun_repo=arun_repo,
        run_service=run_service,
        interview_repo=interview_repo,
        schedule_backend=backend,
    )
    app_run_service = ApplicationRunService(
        app_repo=app_repo,
        arun_repo=arun_repo,
        run_service=run_service,
        authorize=_always_authorize,
        approval_service=approval_service,
        side_effects=side_effects,
    )
    return (
        app_repo,
        arun_repo,
        run_service,
        approval_repo,
        approval_service,
        side_effects,
        app_run_service,
        interview_repo,
    )


async def _create_run(
    svc: ApplicationRunService, app_repo: InMemoryJobApplicationRepository
) -> tuple[AgentRun, ApplicationRun, UUID]:
    job_id = uuid4()
    app = _application(job_id, uuid4())
    await app_repo.save_application(app)
    agent_run, application_run = await svc.create_application_run(_actor(), app.id, None)
    return agent_run, application_run, app.id


def test_update_status_executed_once_and_idempotent() -> None:
    asyncio.run(_update_once())


async def _update_once() -> None:
    app_repo, _ar, _rs, _apr, approval_service, side_effects, svc, _iv = _core_with_side_effects()
    agent_run, _app_run, app_id = await _create_run(svc, app_repo)

    # First approval (APPROVE) applies the status change exactly once.
    first = await approval_service.get_pending_by_run(agent_run.id)
    assert first is not None
    run = await svc.decide_approval(_actor(), first.id, DecisionAction.APPROVE, expected_version=1)
    # SHORTLISTED -> a second (schedule) approval is created, run pauses again.
    assert run.status is RunStatus.WAITING_APPROVAL
    refreshed = await app_repo.get_application(app_id)
    assert refreshed is not None
    assert refreshed.status is ApplicationStatus.SHORTLISTED
    assert refreshed.version == 3  # claim + one transition applied

    # Re-executing the same (now EXECUTED) approval is a no-op.
    reread = await approval_service.get_approval(first.id)
    assert reread is not None
    result = await side_effects.execute_update_status(_actor(), reread)
    assert result.executed is False
    assert result.approval_status is ApprovalStatus.EXECUTED
    refreshed2 = await app_repo.get_application(app_id)
    assert refreshed2 is not None
    assert refreshed2.status is ApplicationStatus.SHORTLISTED
    assert refreshed2.version == 3  # unchanged: no second transition


def test_non_shortlisted_skips_second_approval() -> None:
    asyncio.run(_non_shortlisted())


async def _non_shortlisted() -> None:
    app_repo, _ar, _rs, _apr, approval_service, _se, svc, _iv = _core_with_side_effects()
    agent_run, _app_run, app_id = await _create_run(svc, app_repo)

    first = await approval_service.get_pending_by_run(agent_run.id)
    assert first is not None
    # EDIT to ON_HOLD: no interview branch, run completes, no second approval.
    run = await svc.decide_approval(
        _actor(),
        first.id,
        DecisionAction.EDIT,
        expected_version=1,
        edited_params={"target_status": "ON_HOLD"},
    )
    assert run.status is RunStatus.COMPLETED
    refreshed = await app_repo.get_application(app_id)
    assert refreshed is not None
    assert refreshed.status is ApplicationStatus.ON_HOLD
    # No schedule approval was created.
    second = await approval_service.get_pending_by_run(run.id)
    assert second is None


def test_wrong_action_rejected() -> None:
    asyncio.run(_wrong_action())


async def _wrong_action() -> None:
    app_repo, _ar, _rs, _apr, approval_service, side_effects, svc, _iv = _core_with_side_effects()
    agent_run, _app_run, _app_id = await _create_run(svc, app_repo)
    first = await approval_service.get_pending_by_run(agent_run.id)
    assert first is not None
    await svc.decide_approval(_actor(), first.id, DecisionAction.APPROVE, expected_version=1)

    # Calling the schedule executor with a status-change approval is rejected.
    executed = await approval_service.get_approval(first.id)
    assert executed is not None
    proposal = ScheduleProposal(application_id=_app_id, duration_minutes=45)
    try:
        await side_effects.execute_create_schedule(_actor(), executed, proposal=proposal)
        raise AssertionError("expected APPROVAL_WRONG_ACTION")
    except AppError as exc:
        assert exc.code == "APPROVAL_WRONG_ACTION"
        assert exc.http_status == 409


def test_schedule_failure_then_retry_succeeds() -> None:
    asyncio.run(_failure_retry())


class _FlakyScheduleBackend:
    """Fails the first create, then succeeds (simulates a transient outage)."""

    def __init__(self) -> None:
        self.calls = 0
        self._store: dict[str, ScheduleResult] = {}

    async def get_by_idempotency_key(self, key: str) -> ScheduleResult | None:
        return self._store.get(key)

    async def create(
        self, *, proposal: ScheduleProposal, idempotency_key: str, application_id: UUID
    ) -> ScheduleResult:
        self.calls += 1
        if self.calls == 1:
            raise _RetryableScheduleError("schedule backend outage")
        result = ScheduleResult(
            external_schedule_id=f"mock-ok-{self.calls}",
            status=ScheduleStatus.SCHEDULED,
            application_id=application_id,
            created_at=datetime.now(tz=UTC),
            proposal=proposal,
        )
        self._store[idempotency_key] = result
        return result


async def _failure_retry() -> None:
    app_repo, _ar, _rs, _apr, approval_service, side_effects, svc, interview_repo = (
        _core_with_side_effects(schedule_backend=_FlakyScheduleBackend())
    )
    agent_run, _app_run, app_id = await _create_run(svc, app_repo)
    first = await approval_service.get_pending_by_run(agent_run.id)
    assert first is not None
    await svc.decide_approval(_actor(), first.id, DecisionAction.APPROVE, expected_version=1)

    # Second approval triggers the schedule executor; the backend fails once.
    second = await approval_service.get_pending_by_run(agent_run.id)
    assert second is not None and second.action_type is ApprovalActionType.CREATE_INTERVIEW_SCHEDULE
    run = await svc.decide_approval(_actor(), second.id, DecisionAction.APPROVE, expected_version=1)
    # Run is retryable-FAILED and the slot is freed (§11.7/§11.8).
    assert run.status is RunStatus.FAILED
    assert run.retryable is True
    failed = await approval_service.get_approval(second.id)
    assert failed is not None
    assert failed.status is ApprovalStatus.EXECUTION_FAILED
    refreshed = await app_repo.get_application(app_id)
    assert refreshed is not None
    assert refreshed.active_application_run_id is None

    # Retry the SAME approval (same idempotency key): the backend now succeeds.
    proposal = ScheduleProposal(application_id=app_id, duration_minutes=45)
    result = await side_effects.execute_create_schedule(_actor(), second, proposal=proposal)
    assert result.executed is True
    assert result.approval_status is ApprovalStatus.EXECUTED
    healed = await approval_service.get_approval(second.id)
    assert healed is not None
    assert healed.status is ApprovalStatus.EXECUTED
    refreshed2 = await app_repo.get_application(app_id)
    assert refreshed2 is not None
    assert refreshed2.status is ApplicationStatus.INTERVIEW_SCHEDULED
    # The interview is bound to the approval and is idempotent on re-execute.
    interview = await interview_repo.get_by_approval(second.id)
    assert interview is not None
    again = await side_effects.execute_create_schedule(_actor(), second, proposal=proposal)
    assert again.executed is False
    assert again.execution_result == {"external_schedule_id": interview.external_schedule_id}
