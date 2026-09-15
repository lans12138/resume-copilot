"""ApplicationRun lifecycle service (IMP-021): active slot, graph, 409.

The create transaction is the gate the detailed design §11.3 and G4 require:

1. resolve the application and authorize the actor on its job;
2. (optionally) verify the triggering MatchReport belongs to this application;
3. insert ``AgentRun`` and its ``ApplicationRun`` child in the open transaction;
4. **atomically claim the exclusive slot** — ``claim_active_run`` returns
   ``APPLICATION_RUN_ALREADY_ACTIVE``/409 the instant the slot is taken, so two
   concurrent creates for the same application end with exactly one committed
   success (a losing SQL transaction rolls its provisional rows back);
5. commit the ``CREATED`` fact, then let the worker execute the fixed graph to
   ``WAITING_APPROVAL`` while the slot stays occupied.

The terminal-state clear (``clear_active_run``) is only ever triggered when the
run reaches CANCELLED/FAILED and the slot *still* points at this run, so a stale
attempt can never wipe a newer run's slot (§11).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

from backend.app.agent.enqueuer import ExecutionIntent, RunEnqueuer
from backend.app.agent.models import AgentRun, RunStatus, RunType
from backend.app.agent.service import RunService
from backend.app.approvals.models import Approval, ApprovalActionType, ApprovalStatus
from backend.app.approvals.service import ApprovalService, DecisionAction
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError, app_error
from backend.app.interviews.schemas import ScheduleProposal
from backend.app.job_applications.graph import build_application_graph
from backend.app.job_applications.models import ApplicationRun, JobApplication
from backend.app.job_applications.repository import (
    ApplicationRunRepository,
    JobApplicationRepository,
)
from backend.app.job_applications.side_effects import ApplicationSideEffectService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApplicationRunDelivery:
    """One post-commit ApplicationRun execution publication."""

    run_id: UUID
    attempt: int
    intent: ExecutionIntent
    actor_id: UUID
    resume_version: str | None = None
    approval_id: UUID | None = None


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
        approval_service: ApprovalService,
        report_lookup: MatchReportLookup | None = None,
        side_effects: ApplicationSideEffectService | None = None,
        enqueuer: RunEnqueuer | None = None,
        defer_execution: bool = False,
    ) -> None:
        self._app_repo = app_repo
        self._arun_repo = arun_repo
        self._run_service = run_service
        self._authorize = authorize
        self._approval_service = approval_service
        self._report_lookup = report_lookup
        # Optional: when None, decide_approval drives the graph without applying
        # the side effect (used by the IMP-022 state-machine tests).
        self._side_effects = side_effects
        self._enqueuer = enqueuer
        self._defer_execution = defer_execution
        self._pending_deliveries: list[ApplicationRunDelivery] = []

    def publish_pending_deliveries(self) -> None:
        """Publish work only after the surrounding transaction committed.

        A broker outage must not turn an already committed business fact into a
        5xx. FIN-006's republisher recovers the durable CREATED/RUNNING state.
        """
        deliveries, self._pending_deliveries = self._pending_deliveries, []
        if self._enqueuer is None:
            return
        for delivery in deliveries:
            try:
                self._enqueuer.enqueue_application_run(
                    delivery.run_id,
                    attempt=delivery.attempt,
                    intent=delivery.intent,
                    actor_id=delivery.actor_id,
                    resume_version=delivery.resume_version,
                    approval_id=delivery.approval_id,
                )
            except Exception:  # noqa: BLE001 - durable fact is republishable
                logger.warning(
                    "application_run.publish_failed run_id=%s intent=%s",
                    delivery.run_id,
                    delivery.intent.value,
                    exc_info=True,
                )

    def _stage_delivery(
        self,
        run: AgentRun,
        actor: Actor,
        intent: ExecutionIntent,
        *,
        resume_version: str | None = None,
        approval_id: UUID | None = None,
    ) -> None:
        self._pending_deliveries.append(
            ApplicationRunDelivery(
                run_id=run.id,
                attempt=run.attempt,
                intent=intent,
                actor_id=actor.user_id,
                resume_version=resume_version,
                approval_id=approval_id,
            )
        )

    async def create_application_run(
        self,
        actor: Actor,
        application_id: UUID,
        match_report_id: UUID | None = None,
        *,
        attempt: int = 1,
    ) -> tuple[AgentRun, ApplicationRun]:
        """Claim the slot and persist a CREATED run for worker execution.

        ``attempt`` is 1 for a fresh run and ``parent.attempt + 1`` for a retry
        after an ``APPROVAL_EXPIRED`` timeout (§11.8): the expired run is FAILED
        with ``retryable=True`` and its slot cleared, so a new attempt can reclaim
        it and create a *new* approval (the TIMEOUT retry rule, not a re-decide).
        """
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

        # The active-slot composite FK points at ApplicationRun, so persist both
        # run rows in the open transaction before the conditional UPDATE. A
        # concurrent loser rolls these provisional rows back with its request.
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
            attempt=attempt,
            created_by=actor.user_id,
            start_immediately=False,
        )
        application_run = ApplicationRun(
            run_id=run_id,
            application_id=application_id,
            match_report_id=match_report_id,
        )
        await self._arun_repo.save_application_run(application_run)

        # Atomically claim the slot; raises 409 if already occupied.
        await self._app_repo.claim_active_run(application_id, run_id)

        if self._defer_execution:
            self._stage_delivery(run, actor, ExecutionIntent.START)
        else:
            await self.execute_application_run(
                actor, run, application_run, intent=ExecutionIntent.START
            )
        return run, application_run

    async def execute_application_run(
        self,
        actor: Actor,
        run: AgentRun,
        application_run: ApplicationRun,
        *,
        intent: ExecutionIntent,
        resume_version: str | None = None,
        resume_approval: Approval | None = None,
    ) -> None:
        """Execute one worker-owned graph slice under the caller's row lock."""
        if run.run_type is not RunType.APPLICATION:
            raise RuntimeError("application worker received a non-application run")
        application = await self._app_repo.get_application(application_run.application_id)
        if application is None:
            raise app_error("APPLICATION_NOT_FOUND", http_status=404, safe_message="投递不存在")
        await self._authorize(actor, application.job_id)

        if intent is ExecutionIntent.START:
            if run.created_by is not None and run.created_by != actor.user_id:
                raise RuntimeError("application start actor does not match run creator")
            await self._run_service.emit_status(
                run,
                status=RunStatus.RUNNING,
                message_key="run.worker_started",
            )
            initial_state: dict[str, Any] = {
                "application_id": str(application.id),
                "job_id": str(application.job_id),
                "candidate_id": str(application.candidate_id),
                "current_status": application.status.value,
                "match_report_id": (
                    str(application_run.match_report_id)
                    if application_run.match_report_id is not None
                    else None
                ),
                "attempt": run.attempt,
                "proposed_status": "SHORTLISTED",
            }
            result = await self._run_service.run_graph(
                run, build_application_graph(), initial_state
            )
            if result is None or run.status is not RunStatus.WAITING_APPROVAL:
                raise RuntimeError("initial ApplicationRun did not reach its approval gate")
            proposal = result.checkpoint.checkpoint.get("proposal") or {
                "original_params": {"target_status": "SHORTLISTED"}
            }
            proposed_params = (
                proposal.get("original_params", {})
                if isinstance(proposal, dict)
                else {}
            )
            await self._approval_service.create_approval(
                actor,
                agent_run=run,
                application_run=application_run,
                application=application,
                action_type=ApprovalActionType.UPDATE_APPLICATION_STATUS,
                ordinal=1,
                proposed_params=proposed_params,
            )
            return

        if intent is not ExecutionIntent.RESUME:
            raise RuntimeError(f"unsupported ApplicationRun intent: {intent.value}")
        latest = await self._run_service.get_latest_checkpoint(run)
        if latest is None or resume_version is None:
            raise RuntimeError("ApplicationRun resume has no durable checkpoint")
        if latest.checkpoint_id != resume_version:
            raise RuntimeError("ApplicationRun resume checkpoint is stale")
        if (
            resume_approval is None
            or resume_approval.application_run_id != run.id
            or resume_approval.decided_by != actor.user_id
            or resume_approval.status
            not in {ApprovalStatus.APPROVED, ApprovalStatus.EDITED, ApprovalStatus.EXECUTED}
        ):
            raise RuntimeError("ApplicationRun resume is not backed by a decided approval")

        state_override: dict[str, Any] | None = None
        if resume_approval.action_type is ApprovalActionType.UPDATE_APPLICATION_STATUS:
            state_override = {"proposed_status": self._decided_target(resume_approval)}
        result = await self._run_service.resume_run(
            run, build_application_graph(), state_override=state_override
        )
        if run.status is RunStatus.WAITING_APPROVAL:
            if result is None:
                raise RuntimeError("paused ApplicationRun did not return a checkpoint")
            question_set = result.checkpoint.checkpoint.get("question_set")
            if isinstance(question_set, dict):
                application_run.question_set_json = question_set
                application_run.question_schema_version = str(
                    question_set.get("schema_version", "v1")
                )
                await self._arun_repo.save_application_run(application_run)
            await self._approval_service.create_approval(
                actor,
                agent_run=run,
                application_run=application_run,
                application=application,
                action_type=ApprovalActionType.CREATE_INTERVIEW_SCHEDULE,
                ordinal=2,
                proposed_params={
                    "duration_minutes": 45,
                    "timezone": "UTC",
                    "interviewer_label": "Hiring Manager",
                },
            )
            return

        if run.status is not RunStatus.COMPLETED:
            raise RuntimeError("ApplicationRun resume did not reach a legal boundary")
        application_run.completion_reason = "SUCCESS"
        await self._arun_repo.save_application_run(application_run)
        await self._app_repo.clear_active_run(application_run.application_id, run.id)

    async def decide_approval(
        self,
        actor: Actor,
        approval_id: UUID,
        decision: DecisionAction,
        *,
        expected_version: int,
        edited_params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> AgentRun:
        """Decide a pending approval, apply its side effect, and schedule resume.

        Two-approval flow (detailed design §10/§11):

        * ``REJECTED`` — completes the run without a side effect; the slot is freed.
        * ``UPDATE_APPLICATION_STATUS`` (first approval) — applies the status change,
          then publishes a worker resume. If the target is ``SHORTLISTED`` the graph pauses
          a second time at ``wait_schedule_approval`` and a ``CREATE_INTERVIEW_SCHEDULE``
          approval is created (slot held); otherwise the run completes and the slot
          is cleared.
        * ``CREATE_INTERVIEW_SCHEDULE`` (second approval) — applies the mock schedule,
          then publishes the terminal resume. A retryable execution
          failure leaves the run retryable-FAILED and frees the slot.

        The side effect is applied by ``ApplicationSideEffectService`` exactly once,
        gated by the approval idempotency key; this method only orchestrates.
        """
        approval, status = await self._approval_service.decide(
            actor,
            approval_id,
            decision,
            expected_version=expected_version,
            edited_params=edited_params,
            idempotency_key=idempotency_key,
        )
        application_run = await self._arun_repo.get_application_run(approval.application_run_id)
        if application_run is None:
            raise app_error("APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")
        application = await self._app_repo.get_application(application_run.application_id)
        if application is None:
            raise app_error("APPLICATION_NOT_FOUND", http_status=404, safe_message="投递不存在")
        run = await self._run_service.get_run(approval.application_run_id)
        if run is None:
            raise app_error("APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")

        if status == ApprovalStatus.REJECTED:
            await self._app_repo.clear_active_run(
                application_run.application_id, approval.application_run_id
            )
            return run

        if approval.action_type is ApprovalActionType.UPDATE_APPLICATION_STATUS:
            if self._side_effects is not None:
                await self._side_effects.execute_update_status(actor, approval)
            await self._stage_resume(run, actor, approval)
            return run

        if approval.action_type is ApprovalActionType.CREATE_INTERVIEW_SCHEDULE:
            if self._side_effects is not None:
                proposal = self._build_schedule_proposal(application, approval)
                try:
                    await self._side_effects.execute_create_schedule(
                        actor, approval, proposal=proposal
                    )
                except AppError as error:
                    if error.code != "SIDE_EFFECT_RETRYABLE_FAILURE":
                        raise
                    # Retryable execution failure: the run is retryable-FAILED and
                    # the slot is freed so a new attempt can retry (§11.7/§11.8).
                    await self._app_repo.clear_active_run(
                        application_run.application_id, approval.application_run_id
                    )
                    return run
            await self._stage_resume(run, actor, approval)
            return run

        raise app_error(
            "APPROVAL_WRONG_ACTION",
            http_status=409,
            safe_message="未知审批类型",
            details={"action_type": approval.action_type.value},
        )

    async def _stage_resume(self, run: AgentRun, actor: Actor, approval: Approval) -> None:
        checkpoint = await self._run_service.get_latest_checkpoint(run)
        if checkpoint is None:
            raise RuntimeError("approved ApplicationRun has no checkpoint")
        if self._defer_execution:
            self._stage_delivery(
                run,
                actor,
                ExecutionIntent.RESUME,
                resume_version=checkpoint.checkpoint_id,
                approval_id=approval.id,
            )
        else:
            application_run = await self._arun_repo.get_application_run(run.id)
            if application_run is None:
                raise RuntimeError("ApplicationRun child disappeared before resume")
            await self.execute_application_run(
                actor,
                run,
                application_run,
                intent=ExecutionIntent.RESUME,
                resume_version=checkpoint.checkpoint_id,
                resume_approval=approval,
            )

    @staticmethod
    def _decided_target(approval: Approval) -> str:
        params = approval.final_params_json or approval.original_params_json or {}
        return str(params.get("target_status", "SHORTLISTED")).upper()

    @staticmethod
    def _build_schedule_proposal(
        application: JobApplication, approval: Approval
    ) -> ScheduleProposal:
        params = approval.final_params_json or approval.original_params_json or {}
        return ScheduleProposal(
            application_id=application.id,
            duration_minutes=int(params.get("duration_minutes", 45)),
            timezone=str(params.get("timezone", "UTC")),
            interviewer_label=str(params.get("interviewer_label", "Hiring Manager")),
        )

    async def get_application_run_detail(
        self, run_id: UUID
    ) -> tuple[AgentRun, ApplicationRun, Approval | None]:
        """Return the run, its child row, and the current PENDING approval (§12.5)."""
        agent_run = await self._run_service.get_run(run_id)
        if agent_run is None:
            raise app_error("APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")
        application_run = await self._arun_repo.get_application_run(run_id)
        if application_run is None:
            raise app_error("APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")
        pending = await self._approval_service.get_pending_by_run(run_id)
        return agent_run, application_run, pending

    async def get_approval_for_actor(
        self, actor: Actor, approval_id: UUID
    ) -> Approval:
        """Expose an authorized approval read for the shared API transaction."""
        return await self._approval_service.get_approval_for_actor(actor, approval_id)

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
        # Collaborative cancellation: a PENDING approval rides into EXPIRED with
        # reason RUN_CANCELLED so the (now cancelled) run can never be decided and
        # is not retryable (§11.9 step 4). An already-decided/expired approval is
        # left untouched by mark_expired's idempotent guard.
        pending = await self._approval_service.get_pending_by_run(run_id)
        if pending is not None and pending.status == ApprovalStatus.PENDING:
            await self._approval_service.mark_expired(pending.id, reason="RUN_CANCELLED")
        return agent_run, application_run

    async def retry_application_run(
        self, actor: Actor, run_id: UUID
    ) -> tuple[AgentRun, ApplicationRun]:
        """Retry a FAILED retryable run as a new attempt (§11.8 step 5).

        A ``TIMEOUT``-expired run is left FAILED with ``retryable=True`` and its
        slot cleared, so a fresh attempt can reclaim the slot and create a *new*
        approval. The old run's errors are left in place (its history is kept); the
        new attempt starts clean. A non-retryable or non-FAILED run is rejected with
        ``RUN_NOT_RETRYABLE``/409, guarding against double execution.
        """
        parent = await self.get_application_run(run_id)
        if parent is None:
            raise app_error("APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")
        agent_run, application_run = parent
        if agent_run.status != RunStatus.FAILED or not agent_run.retryable:
            raise app_error(
                "RUN_NOT_RETRYABLE",
                http_status=409,
                safe_message="该流程不可重试",
                details={"status": agent_run.status.value, "retryable": agent_run.retryable},
            )
        # Reclaim the slot (now free after the timeout clear) under a new attempt.
        return await self.create_application_run(
            actor, application_run.application_id, attempt=agent_run.attempt + 1
        )

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
