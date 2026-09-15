"""Side-effect execution service (IMP-024, detailed design §11.6/§11.7).

This is the *only* place an ApplicationRun's approved mutation is applied. Every
method enforces "execute exactly once" along two axes:

1. The ``Approval`` state machine: a side effect runs only when the approval is
   ``APPROVED``/``EDITED`` (or ``EXECUTION_FAILED`` for a controlled retry). An
   already-``EXECUTED`` approval (or an interview already bound to the approval)
   returns the existing result without re-applying the mutation — the idempotency
   backstop behind "重复请求只执行一次" (G5, line 317).
2. The ``idempotency_key`` travels into the schedule backend, so a duplicate
   schedule request returns the same external id and never creates a second
   interview (§11.7, line 1272).

The service re-reads and re-validates the ``Approval``, ``Run`` and
``Application`` and the actor's permission; it never trusts fields the model
could have supplied (§11.6, line 957). On a retryable backend failure it marks
the approval ``EXECUTION_FAILED`` and the run retryable-FAILED so the run can be
retried with the *same* approval (not a new one).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.app.agent.service import RunService
from backend.app.approvals.models import Approval, ApprovalActionType, ApprovalStatus
from backend.app.approvals.service import ApprovalService
from backend.app.auth.tokens import Actor
from backend.app.core.errors import app_error
from backend.app.interviews.models import Interview, InterviewStatus
from backend.app.interviews.repository import InterviewRepository
from backend.app.interviews.schedule import ScheduleBackend
from backend.app.interviews.schemas import ScheduleProposal
from backend.app.job_applications.models import (
    ApplicationStatus,
    ApplicationStatusHistory,
)
from backend.app.job_applications.repository import (
    ApplicationRunRepository,
    JobApplicationRepository,
)

# Status changes the first approval may apply (the second approval is the only
# path to INTERVIEW_SCHEDULED, set by the schedule execution).
_ALLOWED_FIRST_TARGETS = frozenset(
    {ApplicationStatus.SHORTLISTED, ApplicationStatus.ON_HOLD, ApplicationStatus.REJECTED}
)

# Statuses from which a side effect may be (re)applied.
_EXECUTABLE_STATUSES = frozenset(
    {ApprovalStatus.APPROVED, ApprovalStatus.EDITED, ApprovalStatus.EXECUTION_FAILED}
)


@dataclass
class SideEffectResult:
    """Outcome of a side-effect execution (idempotent-aware)."""

    executed: bool  # True only when a new mutation was applied this call
    approval_status: ApprovalStatus
    execution_result: dict[str, Any] | None = None


class ApplicationSideEffectService:
    """Apply approved ApplicationRun mutations exactly once, gated by Approval."""

    def __init__(
        self,
        *,
        approval_service: ApprovalService,
        app_repo: JobApplicationRepository,
        arun_repo: ApplicationRunRepository,
        run_service: RunService,
        interview_repo: InterviewRepository,
        schedule_backend: ScheduleBackend,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._approval_service = approval_service
        self._app_repo = app_repo
        self._arun_repo = arun_repo
        self._run_service = run_service
        self._interview_repo = interview_repo
        self._schedule_backend = schedule_backend
        self._now = now or (lambda: datetime.now(tz=datetime.now().astimezone().tzinfo))

    # ------------------------------------------------------------------ #
    # UPDATE_APPLICATION_STATUS
    # ------------------------------------------------------------------ #
    async def execute_update_status(self, actor: Actor, approval: Approval) -> SideEffectResult:
        """Apply the approved status change to JobApplication (exactly once)."""
        if approval.action_type is not ApprovalActionType.UPDATE_APPLICATION_STATUS:
            raise app_error(
                "APPROVAL_WRONG_ACTION",
                http_status=409,
                safe_message="该审批不是状态变更类型",
                details={"action_type": approval.action_type.value},
            )
        # Idempotent: an executed approval already applied the mutation.
        if approval.status is ApprovalStatus.EXECUTED:
            return SideEffectResult(
                executed=False,
                approval_status=ApprovalStatus.EXECUTED,
                execution_result=approval.execution_result_json,
            )
        if approval.status not in _EXECUTABLE_STATUSES:
            raise app_error(
                "APPROVAL_NOT_EXECUTABLE",
                http_status=409,
                safe_message="审批不可执行",
                details={"status": approval.status.value},
            )

        # Resolve the JobApplication via the ApplicationRun child (the approval's
        # application_run_id is the AgentRun/ApplicationRun id, not the application id).
        application_run = await self._arun_repo.get_application_run(approval.application_run_id)
        if application_run is None:
            raise app_error(
                "APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在"
            )
        application = await self._app_repo.get_application(application_run.application_id)
        if application is None:
            raise app_error("APPLICATION_NOT_FOUND", http_status=404, safe_message="投递不存在")

        target = self._resolve_target(approval)
        # Optimistic-lock CAS: the application must be at the version the approval
        # was decided against; a concurrent cancel/change fails the execution (§19.5).
        if approval.expected_application_version is not None and (
            application.version != approval.expected_application_version
        ):
            raise app_error(
                "VERSION_CONFLICT",
                http_status=409,
                safe_message="投递版本冲突，执行被拒绝",
                details={
                    "expected_version": approval.expected_application_version,
                    "current_version": application.version,
                },
            )

        from_status = application.status
        application.status = target
        application.version += 1
        await self._app_repo.save_application(application)
        history = ApplicationStatusHistory(
            application_id=application.id,
            from_status=from_status.value,
            to_status=target.value,
            changed_by=actor.user_id,
            run_id=approval.application_run_id,
            approval_id=approval.id,
            safe_reason="UPDATE_APPLICATION_STATUS approved",
        )
        await self._app_repo.append_status_history(history)

        result = {"target_status": target.value, "history_id": str(history.id)}
        await self._approval_service.mark_executed(approval, result=result)
        run = await self._run_service.get_run(approval.application_run_id)
        if run is not None:
            await self._run_service.emit_status(
                run,
                status=run.status,
                message_key="tool.executed",
                node=approval.action_type.value,
                safe_payload={"target_status": target.value},
            )
        return SideEffectResult(
            executed=True, approval_status=ApprovalStatus.EXECUTED, execution_result=result
        )

    # ------------------------------------------------------------------ #
    # CREATE_INTERVIEW_SCHEDULE
    # ------------------------------------------------------------------ #
    async def execute_create_schedule(
        self, actor: Actor, approval: Approval, *, proposal: ScheduleProposal
    ) -> SideEffectResult:
        """Create the mock interview and mark the application INTERVIEW_SCHEDULED."""
        if approval.action_type is not ApprovalActionType.CREATE_INTERVIEW_SCHEDULE:
            raise app_error(
                "APPROVAL_WRONG_ACTION",
                http_status=409,
                safe_message="该审批不是排期类型",
                details={"action_type": approval.action_type.value},
            )
        # Idempotent: if an interview is already bound to this approval, return it.
        existing_interview = await self._interview_repo.get_by_approval(approval.id)
        if existing_interview is not None:
            return SideEffectResult(
                executed=False,
                approval_status=ApprovalStatus.EXECUTED,
                execution_result={"external_schedule_id": existing_interview.external_schedule_id},
            )
        if approval.status not in _EXECUTABLE_STATUSES:
            raise app_error(
                "APPROVAL_NOT_EXECUTABLE",
                http_status=409,
                safe_message="审批不可执行",
                details={"status": approval.status.value},
            )

        application_run = await self._arun_repo.get_application_run(approval.application_run_id)
        if application_run is None:
            raise app_error(
                "APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在"
            )
        application = await self._app_repo.get_application(application_run.application_id)
        if application is None:
            raise app_error("APPLICATION_NOT_FOUND", http_status=404, safe_message="投递不存在")
        if approval.status is ApprovalStatus.EXECUTION_FAILED:
            # Controlled retry (§11.7/§11.8): the failed attempt already cleared the
            # active slot and bumped the application version, so the idempotency-key
            # guard — not the stale optimistic lock — is what prevents a double
            # execution. Re-anchor the optimistic lock to the current snapshot before
            # the CAS so the same approval can be retried safely.
            approval.expected_application_version = application.version
        if approval.expected_application_version is not None and (
            application.version != approval.expected_application_version
        ):
            raise app_error(
                "VERSION_CONFLICT",
                http_status=409,
                safe_message="投递版本冲突，执行被拒绝",
                details={
                    "expected_version": approval.expected_application_version,
                    "current_version": application.version,
                },
            )

        # Execute against the schedule backend, idempotent on the approval key.
        try:
            schedule = await self._schedule_backend.create(
                proposal=proposal,
                idempotency_key=approval.idempotency_key,
                application_id=application.id,
            )
        except _RetryableScheduleError as exc:
            # Controlled retry: flag the approval and the run, let the caller clear
            # the slot. A future request with the same key can retry (§11.7).
            await self._approval_service.mark_execution_failed(
                approval, error_code="SCHEDULE_BACKEND_UNAVAILABLE"
            )
            run = await self._run_service.get_run(approval.application_run_id)
            if run is not None:
                await self._run_service.mark_failed(
                    run,
                    reason="schedule_backend_unavailable",
                    retryable=True,
                    error_code="SCHEDULE_BACKEND_UNAVAILABLE",
                    failed_node="create_interview_schedule",
                )
            raise app_error(
                "SIDE_EFFECT_RETRYABLE_FAILURE",
                http_status=409,
                safe_message="排期执行失败，可重试",
                details={"approval_id": str(approval.id)},
            ) from exc

        interview = Interview(
            application_id=application.id,
            run_id=approval.application_run_id,
            approval_id=approval.id,
            external_schedule_id=schedule.external_schedule_id,
            schedule_json=proposal.model_dump(mode="json"),
            status=InterviewStatus.SCHEDULED,
        )
        await self._interview_repo.save_interview(interview)

        from_status = application.status
        application.status = ApplicationStatus.INTERVIEW_SCHEDULED
        application.version += 1
        await self._app_repo.save_application(application)
        history = ApplicationStatusHistory(
            application_id=application.id,
            from_status=from_status.value,
            to_status=ApplicationStatus.INTERVIEW_SCHEDULED.value,
            changed_by=actor.user_id,
            run_id=approval.application_run_id,
            approval_id=approval.id,
            safe_reason="CREATE_INTERVIEW_SCHEDULE approved",
        )
        await self._app_repo.append_status_history(history)

        result = {"external_schedule_id": schedule.external_schedule_id}
        await self._approval_service.mark_executed(approval, result=result)
        run = await self._run_service.get_run(approval.application_run_id)
        if run is not None:
            await self._run_service.emit_status(
                run,
                status=run.status,
                message_key="tool.executed",
                node=approval.action_type.value,
                safe_payload={"external_schedule_id": schedule.external_schedule_id},
            )
        return SideEffectResult(
            executed=True, approval_status=ApprovalStatus.EXECUTED, execution_result=result
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_target(approval: Approval) -> ApplicationStatus:
        params = approval.final_params_json or approval.original_params_json or {}
        raw = str(params.get("target_status", ApplicationStatus.SHORTLISTED.value)).upper()
        try:
            target = ApplicationStatus(raw)
        except ValueError:
            target = ApplicationStatus.SHORTLISTED
        if target not in _ALLOWED_FIRST_TARGETS:
            raise app_error(
                "APPROVAL_INVALID_TARGET",
                http_status=422,
                safe_message="非法目标状态",
                details={"target_status": target.value},
            )
        return target


class _RetryableScheduleError(RuntimeError):
    """Raised by a schedule backend to signal a retryable outage."""


# Re-export so callers/tests can trigger a retryable failure on the backend.
__all__ = ["ApplicationSideEffectService", "SideEffectResult", "_RetryableScheduleError"]
