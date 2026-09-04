"""IMP-022: Approval creation, decide-once, and resume (G4 gate).

Hermetic tests prove the gate the detailed design §11.4/§11.5 requires: an
approval is frozen in PENDING at the human_review pause, decided exactly once
(the second decision is rejected because the status is no longer PENDING), the
optimistic version guards concurrent editors, an expired approval is rejected,
and APPROVED resumes the graph from its checkpoint while REJECTED completes the
run without any side effect. "Resume after restart" builds a *fresh* service
layer over the same in-memory store and checkpointer to stand in for a process
restart (the real PG checkpointer lands in IMP-030, §17.2).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
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
from backend.app.job_applications.models import ApplicationRun, ApplicationStatus, JobApplication
from backend.app.job_applications.repository import (
    InMemoryApplicationRunRepository,
    InMemoryJobApplicationRepository,
)
from backend.app.job_applications.service import ApplicationRunService

FIXED_NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=datetime.now().astimezone().tzinfo)


def _core(
    now: Any = None,
) -> tuple[
    InMemoryAgentRunRepository,
    InMemoryJobApplicationRepository,
    InMemoryApplicationRunRepository,
    RunService,
    InMemoryApprovalRepository,
    ApprovalService,
    ApplicationRunService,
    InMemoryCheckpointer,
]:
    agent_repo = InMemoryAgentRunRepository()
    checkpointer = InMemoryCheckpointer()
    run_service = RunService(agent_repo, checkpointer)
    app_repo = InMemoryJobApplicationRepository()
    arun_repo = InMemoryApplicationRunRepository()
    approval_repo = InMemoryApprovalRepository()
    clock = now or (lambda: FIXED_NOW)

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
    return (
        agent_repo,
        app_repo,
        arun_repo,
        run_service,
        approval_repo,
        approval_service,
        app_run_service,
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
    app_run_service: ApplicationRunService, app_repo: InMemoryJobApplicationRepository
) -> tuple[AgentRun, ApplicationRun, UUID]:
    job_id = uuid4()
    app = _application(job_id, uuid4())
    await app_repo.save_application(app)
    actor = _actor()
    agent_run, application_run = await app_run_service.create_application_run(actor, app.id, None)
    return agent_run, application_run, app.id


def test_pending_approval_created_and_idempotent_on_replay() -> None:
    asyncio.run(_pending_idempotent())


async def _pending_idempotent() -> None:
    _a, app_repo, _r, _rs, _ar, approval_service, svc, _ck = _core()
    agent_run, application_run, app_id = await _create_run(svc, app_repo)

    first = await approval_service.get_pending_by_run(agent_run.id)
    assert first is not None
    assert first.status is ApprovalStatus.PENDING
    assert first.application_run_id == agent_run.id

    # Node replay with the same idempotency key returns the existing approval.
    application = await app_repo.get_application(app_id)
    assert application is not None
    again = await approval_service.create_approval(
        _actor(),
        agent_run=agent_run,
        application_run=application_run,
        application=application,
        action_type=first.action_type,
        ordinal=1,
        proposed_params=first.original_params_json or {},
    )
    assert again.id == first.id
    pending = await approval_service.get_pending_by_run(agent_run.id)
    assert pending is not None and pending.id == first.id


def test_approve_resumes_graph_and_clears_slot() -> None:
    asyncio.run(_approve_resume())


async def _approve_resume() -> None:
    _a, app_repo, _r, _rs, _ar, approval_service, svc, _ck = _core()
    agent_run, _app_run, app_id = await _create_run(svc, app_repo)
    approval = await approval_service.get_pending_by_run(agent_run.id)
    assert approval is not None

    # First approval (status change) resumes the graph to the SECOND gate
    # (SHORTLISTED needs a schedule approval) and the slot stays occupied.
    run = await svc.decide_approval(
        _actor(), approval.id, DecisionAction.APPROVE, expected_version=1
    )
    assert run.status is RunStatus.WAITING_APPROVAL
    refreshed = await app_repo.get_application(app_id)
    assert refreshed is not None
    assert refreshed.active_application_run_id == run.id

    # A second (schedule) approval is now pending; deciding it completes the run
    # and frees the slot.
    second = await approval_service.get_pending_by_run(run.id)
    assert second is not None
    assert second.action_type is ApprovalActionType.CREATE_INTERVIEW_SCHEDULE
    run2 = await svc.decide_approval(_actor(), second.id, DecisionAction.APPROVE, expected_version=1)
    assert run2.status is RunStatus.COMPLETED
    refreshed2 = await app_repo.get_application(app_id)
    assert refreshed2 is not None
    assert refreshed2.active_application_run_id is None


def test_reject_completes_without_side_effect() -> None:
    asyncio.run(_reject_complete())


async def _reject_complete() -> None:
    _a, app_repo, _r, _rs, _ar, approval_service, svc, _ck = _core()
    agent_run, application_run, app_id = await _create_run(svc, app_repo)
    approval = await approval_service.get_pending_by_run(agent_run.id)
    assert approval is not None

    run = await svc.decide_approval(
        _actor(), approval.id, DecisionAction.REJECT, expected_version=1
    )
    assert run.status is RunStatus.COMPLETED
    refreshed_app = await app_repo.get_application(app_id)
    assert refreshed_app is not None
    assert refreshed_app.active_application_run_id is None
    # The application status is unchanged by a rejection (§5.5).
    assert refreshed_app.status is ApplicationStatus.CREATED
    reread = await _r.get_application_run(application_run.run_id)
    assert reread is not None
    assert reread.completion_reason == "ACTION_REJECTED"


def test_decide_exactly_once() -> None:
    asyncio.run(_decide_once())


async def _decide_once() -> None:
    _a, app_repo, _r, _rs, _ar, approval_service, svc, _ck = _core()
    agent_run, _app_run, _app_id = await _create_run(svc, app_repo)
    approval = await approval_service.get_pending_by_run(agent_run.id)
    assert approval is not None
    await svc.decide_approval(_actor(), approval.id, DecisionAction.APPROVE, expected_version=1)
    # A second decision on the now-decided approval is rejected.
    try:
        await svc.decide_approval(_actor(), approval.id, DecisionAction.APPROVE, expected_version=2)
        raise AssertionError("expected APPROVAL_ALREADY_DECIDED")
    except AppError as exc:
        assert exc.code == "APPROVAL_ALREADY_DECIDED"
        assert exc.http_status == 409


def test_version_conflict() -> None:
    asyncio.run(_version_conflict())


async def _version_conflict() -> None:
    _a, app_repo, _r, _rs, _ar, approval_service, svc, _ck = _core()
    agent_run, _app_run, _app_id = await _create_run(svc, app_repo)
    approval = await approval_service.get_pending_by_run(agent_run.id)
    assert approval is not None
    try:
        await svc.decide_approval(
            _actor(), approval.id, DecisionAction.APPROVE, expected_version=99
        )
        raise AssertionError("expected VERSION_CONFLICT")
    except AppError as exc:
        assert exc.code == "VERSION_CONFLICT"
        assert exc.http_status == 409


def test_expired_approval_rejected() -> None:
    asyncio.run(_expired())


async def _expired() -> None:
    _a, app_repo, _r, _rs, _ar, approval_service, svc, _ck = _core()
    agent_run, _app_run, _app_id = await _create_run(svc, app_repo)
    approval = await approval_service.get_pending_by_run(agent_run.id)
    assert approval is not None
    # Force the approval into the past relative to the injected clock.
    approval.expires_at = FIXED_NOW - timedelta(minutes=1)
    try:
        await svc.decide_approval(_actor(), approval.id, DecisionAction.APPROVE, expected_version=1)
        raise AssertionError("expected APPROVAL_EXPIRED")
    except AppError as exc:
        assert exc.code == "APPROVAL_EXPIRED"
        assert exc.http_status == 409


def test_edit_requires_target_status() -> None:
    asyncio.run(_edit_validation())


async def _edit_validation() -> None:
    _a, app_repo, _r, _rs, _ar, approval_service, svc, _ck = _core()
    agent_run, _app_run, _app_id = await _create_run(svc, app_repo)
    approval = await approval_service.get_pending_by_run(agent_run.id)
    assert approval is not None
    try:
        await svc.decide_approval(
            _actor(), approval.id, DecisionAction.EDIT, expected_version=1, edited_params=None
        )
        raise AssertionError("expected APPROVAL_INVALID_EDIT")
    except AppError as exc:
        assert exc.code == "APPROVAL_INVALID_EDIT"
        assert exc.http_status == 422


def test_resume_after_restart_uses_same_thread() -> None:
    asyncio.run(_resume_after_restart())


async def _resume_after_restart() -> None:
    """A fresh service layer over the same store/checkpointer resumes (§17.2)."""
    _a, app_repo, arun_repo, run_service, approval_repo, approval_service, svc, checkpointer = (
        _core()
    )
    agent_run, _app_run, app_id = await _create_run(svc, app_repo)
    approval = await approval_service.get_pending_by_run(agent_run.id)
    assert approval is not None

    # Simulate a process restart: a brand-new service layer, but the *same*
    # checkpointer store, so the run resumes from its WAITING_APPROVAL thread.
    async def authorize2(actor: Actor, job_id: UUID) -> None:
        return None

    run_service2 = RunService(run_service._repository, checkpointer)
    approval_service2 = ApprovalService(
        authorize=authorize2,
        approval_repo=approval_repo,
        arun_repo=arun_repo,
        app_repo=app_repo,
        run_service=run_service2,
        now=lambda: FIXED_NOW,
    )
    svc2 = ApplicationRunService(
        app_repo=app_repo,
        arun_repo=arun_repo,
        run_service=run_service2,
        authorize=authorize2,
        approval_service=approval_service2,
    )

    # First approval (status) resumes to the second gate; decide it too.
    run = await svc2.decide_approval(
        _actor(), approval.id, DecisionAction.APPROVE, expected_version=1
    )
    assert run.status is RunStatus.WAITING_APPROVAL
    second = await approval_service2.get_pending_by_run(run.id)
    assert second is not None
    assert second.action_type is ApprovalActionType.CREATE_INTERVIEW_SCHEDULE
    # Second approval (schedule) — over the restarted service — completes the run.
    run2 = await svc2.decide_approval(
        _actor(), second.id, DecisionAction.APPROVE, expected_version=1
    )
    assert run2.status is RunStatus.COMPLETED
    refreshed = await app_repo.get_application(app_id)
    assert refreshed is not None
    assert refreshed.active_application_run_id is None
