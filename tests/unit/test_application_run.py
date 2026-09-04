"""IMP-021: ApplicationRun active slot, graph, and 409 on concurrent create.

Hermetic unit tests use the in-memory adapters with no database. The exclusive
slot is exercised concurrently to prove exactly one of two simultaneous creates
succeeds and the loser gets APPLICATION_RUN_ALREADY_ACTIVE/409 (G4).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

from backend.app.agent.checkpoint import InMemoryCheckpointer
from backend.app.agent.models import AgentRun, RunStatus
from backend.app.agent.repository import InMemoryAgentRunRepository
from backend.app.agent.service import RunService
from backend.app.approvals.models import ApprovalStatus
from backend.app.approvals.repository import InMemoryApprovalRepository
from backend.app.approvals.service import ApprovalService
from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError
from backend.app.job_applications.models import ApplicationRun, ApplicationStatus, JobApplication
from backend.app.job_applications.repository import (
    InMemoryApplicationRunRepository,
    InMemoryJobApplicationRepository,
)
from backend.app.job_applications.service import ApplicationRunService


def _make_core() -> tuple[
    InMemoryAgentRunRepository,
    InMemoryJobApplicationRepository,
    InMemoryApplicationRunRepository,
    RunService,
    InMemoryApprovalRepository,
]:
    agent_repo = InMemoryAgentRunRepository()
    checkpointer = InMemoryCheckpointer()
    run_service = RunService(agent_repo, checkpointer)
    app_repo = InMemoryJobApplicationRepository()
    arun_repo = InMemoryApplicationRunRepository()
    approval_repo = InMemoryApprovalRepository()
    return agent_repo, app_repo, arun_repo, run_service, approval_repo


def _build_approval_service(
    app_repo: InMemoryJobApplicationRepository,
    arun_repo: InMemoryApplicationRunRepository,
    run_service: RunService,
    approval_repo: InMemoryApprovalRepository,
) -> ApprovalService:
    return ApprovalService(
        authorize=_always_authorize,
        approval_repo=approval_repo,
        arun_repo=arun_repo,
        app_repo=app_repo,
        run_service=run_service,
    )


async def _always_authorize(actor: Actor, job_id: UUID) -> None:
    return None


def _service(
    app_repo: InMemoryJobApplicationRepository,
    arun_repo: InMemoryApplicationRunRepository,
    run_service: RunService,
    approval_repo: InMemoryApprovalRepository,
    *,
    report_lookup: Any | None = None,
) -> ApplicationRunService:
    approval_service = _build_approval_service(app_repo, arun_repo, run_service, approval_repo)
    return ApplicationRunService(
        app_repo=app_repo,
        arun_repo=arun_repo,
        run_service=run_service,
        authorize=_always_authorize,
        approval_service=approval_service,
        report_lookup=report_lookup,
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


def test_concurrent_create_only_one_succeeds() -> None:
    asyncio.run(_concurrent_create())


async def _concurrent_create() -> None:
    _agent_repo, app_repo, arun_repo, run_service, approval_repo = _make_core()
    service = _service(app_repo, arun_repo, run_service, approval_repo)
    job_id = uuid4()
    app = _application(job_id, uuid4())
    await app_repo.save_application(app)
    actor = _actor()

    async def _create() -> tuple[AgentRun, ApplicationRun]:
        return await service.create_application_run(actor, app.id, None)

    results = await asyncio.gather(_create(), _create(), return_exceptions=True)
    ok = cast(
        "list[tuple[AgentRun, ApplicationRun]]",
        [r for r in results if not isinstance(r, Exception)],
    )
    errs = [r for r in results if isinstance(r, AppError)]
    assert len(ok) == 1, f"expected exactly one winner, got {len(ok)}"
    assert len(errs) == 1, f"expected exactly one 409, got {len(errs)}"
    assert errs[0].code == "APPLICATION_RUN_ALREADY_ACTIVE"
    assert errs[0].http_status == 409
    assert "current_run_id" in errs[0].details

    refreshed = await app_repo.get_application(app.id)
    assert refreshed is not None
    assert refreshed.active_application_run_id == ok[0][0].id


def test_create_reaches_waiting_approval_and_holds_slot() -> None:
    asyncio.run(_create_reaches_waiting_approval())


async def _create_reaches_waiting_approval() -> None:
    _agent_repo, app_repo, arun_repo, run_service, approval_repo = _make_core()
    service = _service(app_repo, arun_repo, run_service, approval_repo)
    job_id = uuid4()
    app = _application(job_id, uuid4())
    await app_repo.save_application(app)
    actor = _actor()

    agent_run, application_run = await service.create_application_run(actor, app.id, None)
    # Graph pauses at human_review in WAITING_APPROVAL; the slot stays occupied
    # until a human decision (IMP-022).
    assert agent_run.status is RunStatus.WAITING_APPROVAL
    refreshed = await app_repo.get_application(app.id)
    assert refreshed is not None
    assert refreshed.active_application_run_id == agent_run.id
    assert application_run.application_id == app.id
    # IMP-022: a PENDING approval is frozen at the human_review pause (§11.4).
    detail = await service.get_application_run_detail(agent_run.id)
    assert detail[2] is not None
    assert detail[2].status is ApprovalStatus.PENDING


def test_cancel_clears_slot() -> None:
    asyncio.run(_cancel_clears_slot())


async def _cancel_clears_slot() -> None:
    _agent_repo, app_repo, arun_repo, run_service, approval_repo = _make_core()
    service = _service(app_repo, arun_repo, run_service, approval_repo)
    app = _application(uuid4(), uuid4())
    await app_repo.save_application(app)
    actor = _actor()

    agent_run, _ = await service.create_application_run(actor, app.id, None)
    await service.cancel_application_run(actor, agent_run.id)
    refreshed = await app_repo.get_application(app.id)
    assert refreshed is not None
    assert refreshed.active_application_run_id is None
    reread = await run_service.get_run(agent_run.id)
    assert reread is not None
    assert reread.status is RunStatus.CANCELLED


def test_mark_failed_clears_slot() -> None:
    asyncio.run(_mark_failed_clears_slot())


async def _mark_failed_clears_slot() -> None:
    _agent_repo, app_repo, arun_repo, run_service, approval_repo = _make_core()
    service = _service(app_repo, arun_repo, run_service, approval_repo)
    app = _application(uuid4(), uuid4())
    await app_repo.save_application(app)
    actor = _actor()

    agent_run, _ = await service.create_application_run(actor, app.id, None)
    await service.mark_failed(actor, agent_run.id, reason="node_error")
    refreshed = await app_repo.get_application(app.id)
    assert refreshed is not None
    assert refreshed.active_application_run_id is None
    reread = await run_service.get_run(agent_run.id)
    assert reread is not None
    assert reread.status is RunStatus.FAILED


def test_idempotent_cancel_at_terminal() -> None:
    asyncio.run(_idempotent_cancel())


async def _idempotent_cancel() -> None:
    _agent_repo, app_repo, arun_repo, run_service, approval_repo = _make_core()
    service = _service(app_repo, arun_repo, run_service, approval_repo)
    app = _application(uuid4(), uuid4())
    await app_repo.save_application(app)
    actor = _actor()

    agent_run, _ = await service.create_application_run(actor, app.id, None)
    first = await service.cancel_application_run(actor, agent_run.id)
    # Second cancel at terminal state returns current facts, no error.
    second = await service.cancel_application_run(actor, agent_run.id)
    assert first[0].status is RunStatus.CANCELLED
    assert second[0].status is RunStatus.CANCELLED
    refreshed = await app_repo.get_application(app.id)
    assert refreshed is not None
    assert refreshed.active_application_run_id is None


def test_list_and_get_application_run() -> None:
    asyncio.run(_list_and_get())


async def _list_and_get() -> None:
    _agent_repo, app_repo, arun_repo, run_service, approval_repo = _make_core()
    service = _service(app_repo, arun_repo, run_service, approval_repo)
    app = _application(uuid4(), uuid4())
    await app_repo.save_application(app)
    actor = _actor()

    agent_run, application_run = await service.create_application_run(actor, app.id, None)
    runs = await service.list_runs(app.id)
    assert len(runs) == 1
    found = await service.get_application_run(agent_run.id)
    assert found is not None
    assert found[0].id == agent_run.id
    assert found[1].application_id == application_run.application_id

    missing = await service.get_application_run(uuid4())
    assert missing is None


def test_report_lookup_rejects_unlinked_report() -> None:
    asyncio.run(_report_lookup_validation())


async def _report_lookup_validation() -> None:
    _agent_repo, app_repo, arun_repo, run_service, approval_repo = _make_core()

    async def lookup_wrong(report_id: UUID) -> Any | None:
        return SimpleNamespace(application_id=uuid4())  # different application

    async def lookup_missing(report_id: UUID) -> Any | None:
        return None

    app = _application(uuid4(), uuid4())
    await app_repo.save_application(app)
    actor = _actor()

    # Wrong application -> 422.
    svc_wrong = _service(
        app_repo, arun_repo, run_service, approval_repo, report_lookup=lookup_wrong
    )
    try:
        await svc_wrong.create_application_run(actor, app.id, uuid4())
        raise AssertionError("expected 422 for unlinked report")
    except AppError as exc:
        assert exc.code == "MATCH_REPORT_NOT_LINKED"
        assert exc.http_status == 422

    # Missing report -> 422.
    svc_missing = _service(
        app_repo, arun_repo, run_service, approval_repo, report_lookup=lookup_missing
    )
    try:
        await svc_missing.create_application_run(actor, app.id, uuid4())
        raise AssertionError("expected 422 for missing report")
    except AppError as exc:
        assert exc.code == "MATCH_REPORT_NOT_LINKED"
        assert exc.http_status == 422

    # Linked report -> succeeds.
    async def lookup_ok(report_id: UUID) -> Any | None:
        return SimpleNamespace(application_id=app.id)

    svc_ok = _service(app_repo, arun_repo, run_service, approval_repo, report_lookup=lookup_ok)
    agent_run, _ = await svc_ok.create_application_run(actor, app.id, uuid4())
    assert agent_run.status is RunStatus.WAITING_APPROVAL
