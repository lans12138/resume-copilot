"""IMP-028: E2E, failure-recovery and concurrency regression.

These hermetic tests freeze the concurrent and restart invariants from
detailed design §19.5 / §19.8 and the G6 gate so a later refactor (e.g. the
real LangGraph + PG Checkpointer swap in IMP-030) cannot silently break them.

Covered here (the rest of §19.5/§19.8 is already pinned by
test_application_run, test_approval, test_side_effects, test_maintenance and
test_sse):

* §19.5#2  cancel racing a MockSchedule execution -> exactly one commit,
            audit sequence stays contiguous.
* §19.5#3  PENDING-approval timeout racing a human decision -> one terminal.
* §19.5#5  a committed write replayed (checkpoint not advanced) is a no-op.
* §19.8#8  a RUNNING/WAITING run cancelled rejects later node/side-effect writes.
* §19.8     the ApplicationRun happy path end-to-end (HR decision -> exactly-once
            side effect -> Interview -> COMPLETED -> slot cleared), proving the
            "core E2E passes" IMP-028 exit criterion at the service layer.

The one §19.5 scenario that needs real Redis + PostgreSQL (Redis flushed while
PostgreSQL facts survive, maintenance republishes QUEUED) is deliberately not
hermetic: it is exercised by the Compose E2E in the G6 CI gate, and its core
invariant -- event storage is decoupled from the notifier -- is already pinned
by test_sse.test_heartbeat_discovers_events_without_notify.
"""

from __future__ import annotations

import asyncio
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
from backend.app.interviews.schemas import ScheduleProposal
from backend.app.job_applications.models import (
    ApplicationRun,
    ApplicationStatus,
    JobApplication,
)
from backend.app.job_applications.repository import (
    InMemoryApplicationRunRepository,
    InMemoryJobApplicationRepository,
)
from backend.app.job_applications.service import ApplicationRunService
from backend.app.job_applications.side_effects import ApplicationSideEffectService


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


def test_concurrent_cancel_vs_schedule_execution() -> None:
    asyncio.run(_concurrent_cancel_vs_schedule())


async def _concurrent_cancel_vs_schedule() -> None:
    """§19.5#2: cancel racing a MockSchedule commit -> one terminal, ordered audit."""
    (
        app_repo,
        _ar,
        run_service,
        _apr,
        approval_service,
        _se,
        svc,
        interview_repo,
    ) = _core_with_side_effects()
    agent_run, _app_run, app_id = await _create_run(svc, app_repo)

    first = await approval_service.get_pending_by_run(agent_run.id)
    assert first is not None
    await svc.decide_approval(_actor(), first.id, DecisionAction.APPROVE, expected_version=1)

    second = await approval_service.get_pending_by_run(agent_run.id)
    assert second is not None
    assert second.action_type is ApprovalActionType.CREATE_INTERVIEW_SCHEDULE

    async def _decide() -> AgentRun:
        return await svc.decide_approval(
            _actor(), second.id, DecisionAction.APPROVE, expected_version=1
        )

    async def _cancel() -> tuple[AgentRun, ApplicationRun]:
        return await svc.cancel_application_run(_actor(), agent_run.id)

    results = await asyncio.gather(_decide(), _cancel(), return_exceptions=True)

    reread = await run_service.get_run(agent_run.id)
    # Exactly one terminal state wins the race.
    assert reread is not None
    assert reread.status in (RunStatus.COMPLETED, RunStatus.CANCELLED)

    if reread.status is RunStatus.COMPLETED:
        # Schedule commit won: an interview exists, the decision path was not cancelled.
        assert not isinstance(results[0], Exception)
        interview = await interview_repo.get_by_approval(second.id)
        assert interview is not None
    else:
        # Cancel won: the schedule decision must have been rejected, no interview.
        interview = await interview_repo.get_by_approval(second.id)
        assert interview is None

    # Audit sequence stays contiguous and duplicate-free under concurrency.
    events = await run_service._repository.list_events(agent_run.id)  # noqa: SLF001
    seqs = [e.sequence for e in events]
    assert len(seqs) == len(set(seqs))  # no duplicate sequence numbers
    assert seqs == sorted(seqs)  # monotonic increasing
    assert seqs[-1] - seqs[0] == len(seqs) - 1  # no gaps between consecutive writes


def test_concurrent_timeout_vs_decision() -> None:
    asyncio.run(_concurrent_timeout_vs_decision())


async def _concurrent_timeout_vs_decision() -> None:
    """§19.5#3: a PENDING approval timed out while a human decides -> one terminal."""
    (
        app_repo,
        _ar,
        _rs,
        _apr,
        approval_service,
        _se,
        svc,
        _iv,
    ) = _core_with_side_effects()
    agent_run, _app_run, _app_id = await _create_run(svc, app_repo)

    first = await approval_service.get_pending_by_run(agent_run.id)
    assert first is not None

    async def _decide() -> tuple[Any, ApprovalStatus]:
        return await approval_service.decide(
            _actor(), first.id, DecisionAction.APPROVE, expected_version=1
        )

    async def _expire() -> Any:
        return await approval_service.mark_expired(first.id, reason="TIMEOUT")

    await asyncio.gather(_decide(), _expire(), return_exceptions=True)

    reread = await approval_service.get_approval(first.id)
    assert reread is not None
    # Mutually exclusive terminals: exactly one effective action landed.
    assert (reread.status is ApprovalStatus.APPROVED) ^ (
        reread.status is ApprovalStatus.EXPIRED
    )


def test_replay_after_committed_write_is_idempotent() -> None:
    asyncio.run(_replay_idempotent())


async def _replay_idempotent() -> None:
    """§19.5#5: a committed write replayed (checkpoint not advanced) is a no-op."""
    (
        app_repo,
        _ar,
        _rs,
        _apr,
        approval_service,
        side_effects,
        svc,
        _iv,
    ) = _core_with_side_effects()
    agent_run, _app_run, app_id = await _create_run(svc, app_repo)

    first = await approval_service.get_pending_by_run(agent_run.id)
    assert first is not None
    await svc.decide_approval(_actor(), first.id, DecisionAction.APPROVE, expected_version=1)

    executed = await approval_service.get_approval(first.id)
    assert executed is not None
    assert executed.status is ApprovalStatus.EXECUTED

    app_before = await app_repo.get_application(app_id)
    assert app_before is not None
    version_before = app_before.version

    # Replay the already-executed approval: no second status transition.
    result = await side_effects.execute_update_status(_actor(), executed)
    assert result.executed is False
    assert result.approval_status is ApprovalStatus.EXECUTED

    app_after = await app_repo.get_application(app_id)
    assert app_after is not None
    assert app_after.version == version_before  # no duplicate status history
    assert app_after.status is ApplicationStatus.SHORTLISTED


def test_cancel_rejects_later_node_writes() -> None:
    asyncio.run(_cancel_rejects_writes())


async def _cancel_rejects_writes() -> None:
    """§19.8#8 + §19.5#1: a cancelled run rejects later node/side-effect writes."""
    (
        app_repo,
        _ar,
        _rs,
        _apr,
        approval_service,
        _se,
        svc,
        _iv,
    ) = _core_with_side_effects()
    agent_run, _app_run, app_id = await _create_run(svc, app_repo)

    first = await approval_service.get_pending_by_run(agent_run.id)
    assert first is not None

    await svc.cancel_application_run(_actor(), agent_run.id)
    reread = await svc.get_application_run(agent_run.id)
    assert reread is not None
    assert reread[0].status is RunStatus.CANCELLED

    app = await app_repo.get_application(app_id)
    assert app is not None
    assert app.active_application_run_id is None
    assert app.status is ApplicationStatus.CREATED  # untouched by the cancelled run

    # A later decision on the now-cancelled run is rejected, not silently applied.
    try:
        await svc.decide_approval(_actor(), first.id, DecisionAction.APPROVE, expected_version=1)
        raise AssertionError("expected the post-cancel decision to be rejected")
    except AppError as exc:
        assert exc.code in ("APPROVAL_RUN_NOT_ACTIVE", "APPROVAL_ALREADY_DECIDED")


def test_e2e_application_run_happy_path() -> None:
    asyncio.run(_e2e_happy_path())


async def _e2e_happy_path() -> None:
    """§19.8 core E2E: HR decisions -> exactly-once side effect -> Interview -> COMPLETED."""
    (
        app_repo,
        _ar,
        _rs,
        _apr,
        approval_service,
        side_effects,
        svc,
        interview_repo,
    ) = _core_with_side_effects()
    agent_run, _app_run, app_id = await _create_run(svc, app_repo)
    actor = _actor()

    # First approval: APPROVE -> SHORTLISTED (no interview branch yet).
    first = await approval_service.get_pending_by_run(agent_run.id)
    assert first is not None
    run = await svc.decide_approval(actor, first.id, DecisionAction.APPROVE, expected_version=1)
    assert run.status is RunStatus.WAITING_APPROVAL

    # Second approval: schedule an interview (the only side-effecting branch).
    second = await approval_service.get_pending_by_run(agent_run.id)
    assert second is not None
    assert second.action_type is ApprovalActionType.CREATE_INTERVIEW_SCHEDULE
    run2 = await svc.decide_approval(actor, second.id, DecisionAction.APPROVE, expected_version=1)
    assert run2.status is RunStatus.COMPLETED

    app = await app_repo.get_application(app_id)
    assert app is not None
    assert app.status is ApplicationStatus.INTERVIEW_SCHEDULED
    assert app.active_application_run_id is None  # slot cleared at terminal

    interview = await interview_repo.get_by_approval(second.id)
    assert interview is not None

    # Exactly-once: replaying the schedule approval must not create a second interview.
    proposal = ScheduleProposal(application_id=app_id, duration_minutes=45)
    again = await side_effects.execute_create_schedule(actor, second, proposal=proposal)
    assert again.executed is False
    assert again.execution_result == {"external_schedule_id": interview.external_schedule_id}
