"""ApplicationRun lifecycle service (IMP-021): active slot, graph, 409.

The create transaction is the gate the detailed design §11.3 and G4 require:

1. resolve the application and authorize the actor on its job;
2. (optionally) verify the triggering MatchReport belongs to this application;
3. **atomically claim the exclusive slot** — ``claim_active_run`` returns
   ``APPLICATION_RUN_ALREADY_ACTIVE``/409 the instant the slot is taken, so two
   concurrent creates for the same application end with exactly one success;
4. insert ``AgentRun`` (CREATED, then RUNNING) and the ``ApplicationRun`` child;
5. execute the fixed graph, which pauses at ``human_review`` in
   ``WAITING_APPROVAL`` while the slot stays occupied.

The terminal-state clear (``clear_active_run``) is only ever triggered when the
run reaches CANCELLED/FAILED and the slot *still* points at this run, so a stale
attempt can never wipe a newer run's slot (§11).
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID, uuid4

from backend.app.agent.models import AgentRun, RunStatus, RunType
from backend.app.agent.service import RunService
from backend.app.auth.tokens import Actor
from backend.app.core.errors import app_error
from backend.app.job_applications.graph import build_application_graph
from backend.app.job_applications.models import ApplicationRun
from backend.app.job_applications.repository import (
    ApplicationRunRepository,
    JobApplicationRepository,
)


class ApplicationAccess(Protocol):
    """Authorize an actor against a job; raise 403/404 on failure."""

    async def __call__(self, actor: Actor, job_id: UUID) -> None: ...


class MatchReportLookup(Protocol):
    """Resolve a MatchReport; return None if missing. Must expose application_id."""

    async def __call__(self, report_id: UUID) -> Any | None: ...


class ApplicationRunService:
    """Create, drive, cancel, and query ApplicationRuns under the active slot."""

    def __init__(
        self,
        *,
        app_repo: JobApplicationRepository,
        arun_repo: ApplicationRunRepository,
        run_service: RunService,
        authorize: ApplicationAccess,
        report_lookup: MatchReportLookup | None = None,
    ) -> None:
        self._app_repo = app_repo
        self._arun_repo = arun_repo
        self._run_service = run_service
        self._authorize = authorize
        self._report_lookup = report_lookup

    async def create_application_run(
        self,
        actor: Actor,
        application_id: UUID,
        match_report_id: UUID | None = None,
    ) -> tuple[AgentRun, ApplicationRun]:
        """Claim the slot and run the graph to WAITING_APPROVAL (409 if taken)."""
        application = await self._app_repo.get_application(application_id)
        if application is None:
            raise app_error("APPLICATION_NOT_FOUND", http_status=404, safe_message="投递不存在")
        await self._authorize(actor, application.job_id)

        if match_report_id is not None and self._report_lookup is not None:
            report = await self._report_lookup(match_report_id)
            if report is None or report.application_id != application_id:
                raise app_error(
                    "MATCH_REPORT_NOT_LINKED",
                    http_status=422,
                    safe_message="引用的匹配报告不属于该投递",
                    details={"match_report_id": str(match_report_id)},
                )

        # Generate the run id first so the slot can be claimed atomically before
        # anything is persisted; the conditional UPDATE decides the winner.
        run_id = uuid4()
        run = await self._run_service.create_run(
            run_type=RunType.APPLICATION,
            config_snapshot={
                "application_id": str(application_id),
                "job_id": str(application.job_id),
                "match_report_id": str(match_report_id) if match_report_id else None,
            },
            run_id=run_id,
            thread_id=run_id.hex,
            attempt=1,
        )
        # Atomically claim the slot; raises 409 if already occupied.
        await self._app_repo.claim_active_run(application_id, run_id)

        application_run = ApplicationRun(
            run_id=run_id,
            application_id=application_id,
            match_report_id=match_report_id,
        )
        await self._arun_repo.save_application_run(application_run)

        graph = build_application_graph()
        initial_state: dict[str, Any] = {
            "application_id": str(application_id),
            "job_id": str(application.job_id),
            "candidate_id": str(application.candidate_id),
            "current_status": application.status.value,
            "match_report_id": str(match_report_id) if match_report_id else None,
            "attempt": 1,
            "proposed_status": "SHORTLISTED",
        }
        await self._run_service.run_graph(run, graph, initial_state)
        return run, application_run

    async def cancel_application_run(
        self, actor: Actor, run_id: UUID
    ) -> tuple[AgentRun, ApplicationRun]:
        """Cancel a run; clear the slot only if it still points at this run."""
        agent_run = await self._run_service.get_run(run_id)
        if agent_run is None:
            raise app_error("APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")
        application_run = await self._arun_repo.get_application_run(run_id)
        if application_run is None:
            raise app_error("APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")
        if agent_run.run_type is not RunType.APPLICATION:
            raise app_error(
                "INVALID_RUN_TYPE", http_status=409, safe_message="该流程不是 ApplicationRun"
            )

        application = await self._app_repo.get_application(application_run.application_id)
        if application is None:
            raise app_error("APPLICATION_NOT_FOUND", http_status=404, safe_message="投递不存在")
        await self._authorize(actor, application.job_id)

        # Idempotent at a terminal state: return current facts, do not re-clear.
        if agent_run.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
            return agent_run, application_run

        await self._run_service.cancel_run(agent_run, reason="user_cancel")
        await self._app_repo.clear_active_run(application_run.application_id, run_id)
        return agent_run, application_run

    async def mark_failed(
        self, actor: Actor, run_id: UUID, *, reason: str
    ) -> tuple[AgentRun, ApplicationRun]:
        """Mark a run FAILED (e.g. worker failure) and clear the slot if still held."""
        agent_run = await self._run_service.get_run(run_id)
        if agent_run is None:
            raise app_error("APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")
        application_run = await self._arun_repo.get_application_run(run_id)
        if application_run is None:
            raise app_error("APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")

        if agent_run.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
            return agent_run, application_run

        await self._run_service.mark_failed(agent_run, reason=reason)
        # Only clear if this run still owns the slot (a newer run may have replaced it).
        application = await self._app_repo.get_application(application_run.application_id)
        if application is not None and application.active_application_run_id == run_id:
            await self._app_repo.clear_active_run(application_run.application_id, run_id)
        return agent_run, application_run

    async def get_application_run(
        self, run_id: UUID
    ) -> tuple[AgentRun, ApplicationRun] | None:
        agent_run = await self._run_service.get_run(run_id)
        if agent_run is None:
            return None
        application_run = await self._arun_repo.get_application_run(run_id)
        if application_run is None:
            return None
        return agent_run, application_run

    async def list_runs(self, application_id: UUID) -> list[tuple[AgentRun, ApplicationRun]]:
        runs = await self._arun_repo.list_by_application(application_id)
        out: list[tuple[AgentRun, ApplicationRun]] = []
        for application_run in runs:
            agent_run = await self._run_service.get_run(application_run.run_id)
            if agent_run is not None:
                out.append((agent_run, application_run))
        return out
