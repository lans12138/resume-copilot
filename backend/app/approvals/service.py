"""Approval creation and decision service (IMP-022).

This is the gate that makes every ApplicationRun side effect auditable and
idempotent (detailed design §11.4/§11.5). ``create_approval`` freezes the agent
proposal into a ``PENDING`` row the instant the run pauses at ``human_review``;
node replay queries the same idempotency key and returns the existing approval
instead of creating a duplicate (the partial unique index is the DB backstop).
``decide`` enforces "decide exactly once": a non-``PENDING`` approval (already
decided, expired, or failed) is rejected, and the optimistic ``version`` guards
concurrent edits.

ApprovalService owns only the approval state machine and the *run-status events*
that accompany a decision. The graph resume and the active-slot clear live in
``ApplicationRunService`` (which calls ``decide`` and then drives the engine),
keeping the two concerns cleanly separated.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from backend.app.agent.models import AgentRun, RunStatus
from backend.app.agent.service import RunService
from backend.app.approvals.models import Approval, ApprovalActionType, ApprovalStatus
from backend.app.approvals.repository import ApprovalRepository
from backend.app.auth.tokens import Actor
from backend.app.core.errors import app_error
from backend.app.job_applications.models import ApplicationRun, JobApplication
from backend.app.job_applications.repository import (
    ApplicationRunRepository,
    JobApplicationRepository,
)

# Default window before a PENDING approval is considered expired (§11.4).
DEFAULT_APPROVAL_TTL = timedelta(minutes=30)


class ApprovalAuthorize(Protocol):
    """Authorize an actor on a job; raise 403/404 on failure."""

    async def __call__(self, actor: Actor, job_id: UUID) -> None: ...


class DecisionAction(StrEnum):
    """Human decision verb submitted to ``POST /approvals/{id}/decision``."""

    APPROVE = "APPROVE"
    EDIT = "EDIT"
    REJECT = "REJECT"


def _decision_to_status(action: DecisionAction) -> ApprovalStatus:
    return {
        DecisionAction.APPROVE: ApprovalStatus.APPROVED,
        DecisionAction.EDIT: ApprovalStatus.EDITED,
        DecisionAction.REJECT: ApprovalStatus.REJECTED,
    }[action]


class ApprovalService:
    """Create and decide approvals; never applies a side effect itself."""

    def __init__(
        self,
        *,
        authorize: ApprovalAuthorize,
        approval_repo: ApprovalRepository,
        arun_repo: ApplicationRunRepository,
        app_repo: JobApplicationRepository,
        run_service: RunService,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._authorize = authorize
        self._approval_repo = approval_repo
        self._arun_repo = arun_repo
        self._app_repo = app_repo
        self._run_service = run_service
        self._now = now or (lambda: datetime.now(tz=datetime.now().astimezone().tzinfo))

    async def get_approval(self, approval_id: UUID) -> Approval | None:
        """Read a single approval (used by the GET detail endpoint)."""
        return await self._approval_repo.get_approval(approval_id)

    async def get_approval_for_actor(self, actor: Actor, approval_id: UUID) -> Approval:
        """Read an approval after re-authorizing the actor on its job (§12.5)."""
        approval = await self._approval_repo.get_approval(approval_id)
        if approval is None:
            raise app_error("APPROVAL_NOT_FOUND", http_status=404, safe_message="审批不存在")
        application_run = await self._arun_repo.get_application_run(approval.application_run_id)
        if application_run is None:
            raise app_error("APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")
        application = await self._app_repo.get_application(application_run.application_id)
        if application is None:
            raise app_error("APPLICATION_NOT_FOUND", http_status=404, safe_message="投递不存在")
        await self._authorize(actor, application.job_id)
        return approval

    async def get_pending_by_run(self, application_run_id: UUID) -> Approval | None:
        """Current PENDING approval for a run (or None); powers ApplicationRunDetail."""
        return await self._approval_repo.get_pending_by_run(application_run_id)

    async def mark_expired(self, approval_id: UUID, *, reason: str) -> Approval | None:
        """Set a PENDING approval to EXPIRED (idempotent); powers cancel + timeout.

        ``reason`` is ``TIMEOUT`` (sweeper, §11.8) or ``RUN_CANCELLED`` (cancel,
        §11.9). A non-PENDING approval (already decided, expired, or failed) is
        left untouched and returned as-is, so concurrent decide/expire calls can
        never apply a second transition (the same "decide exactly once" guard the
        decision path relies on).

        This method does **not** authorize: it is called by internal sweeps and
        by the cancellation flow that has already authorized the actor on the job.
        """
        approval = await self._approval_repo.get_approval(approval_id)
        if approval is None or approval.status != ApprovalStatus.PENDING:
            return approval
        approval.status = ApprovalStatus.EXPIRED
        approval.expiration_reason = reason
        approval.version += 1
        await self._approval_repo.save_approval(approval)
        return approval

    async def create_approval(
        self,
        actor: Actor,
        *,
        agent_run: AgentRun,
        application_run: ApplicationRun,
        application: JobApplication,
        action_type: ApprovalActionType,
        ordinal: int,
        proposed_params: dict[str, Any],
        expires_at: datetime | None = None,
    ) -> Approval:
        """Freeze a PENDING approval at the human_review pause (idempotent on key)."""
        await self._authorize(actor, application.job_id)
        if agent_run.status in (RunStatus.CANCELLED, RunStatus.FAILED):
            raise app_error(
                "APPROVAL_RUN_NOT_ACTIVE",
                http_status=409,
                safe_message="流程已终止，无法创建审批",
            )

        key = f"{agent_run.id}:{agent_run.attempt}:{action_type.value}:{ordinal}"
        existing = await self._approval_repo.get_by_idempotency_key(key)
        if existing is not None:
            return existing  # Node replay returns the existing proposal, no duplicate.

        expires_at = expires_at or (self._now() + DEFAULT_APPROVAL_TTL)
        approval = Approval(
            id=uuid4(),
            application_run_id=application_run.run_id,
            action_type=action_type,
            status=ApprovalStatus.PENDING,
            original_params_json=proposed_params,
            expected_application_version=application.version,
            idempotency_key=key,
            version=1,
            expires_at=expires_at,
        )
        await self._approval_repo.save_approval(approval)
        await self._run_service.emit_status(
            agent_run,
            status=RunStatus.WAITING_APPROVAL,
            message_key="approval.created",
            safe_payload={"approval_id": str(approval.id), "action_type": action_type.value},
        )
        return approval

    async def decide(
        self,
        actor: Actor,
        approval_id: UUID,
        decision: DecisionAction,
        *,
        expected_version: int,
        edited_params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[Approval, ApprovalStatus]:
        """Decide an approval exactly once; migrate the run accordingly.

        Returns the updated approval and the resulting status (APPROVED/EDITED/
        REJECTED). The caller (``ApplicationRunService``) resumes the graph for
        APPROVED/EDITED and clears the active slot for REJECTED.
        """
        approval = await self._approval_repo.get_approval(approval_id)
        if approval is None:
            raise app_error("APPROVAL_NOT_FOUND", http_status=404, safe_message="审批不存在")
        application_run = await self._arun_repo.get_application_run(approval.application_run_id)
        if application_run is None:
            raise app_error("APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")
        agent_run = await self._run_service.get_run(approval.application_run_id)
        if agent_run is None:
            raise app_error("APPLICATION_RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")
        application = await self._app_repo.get_application(application_run.application_id)
        if application is None:
            raise app_error("APPLICATION_NOT_FOUND", http_status=404, safe_message="投递不存在")
        await self._authorize(actor, application.job_id)

        now = self._now()
        if (
            approval.status == ApprovalStatus.PENDING
            and approval.expires_at is not None
            and approval.expires_at <= now
        ):
            raise app_error(
                "APPROVAL_EXPIRED",
                http_status=409,
                safe_message="审批已过期",
                details={"approval_id": str(approval.id)},
            )
        # Decide exactly once: any non-PENDING approval rejects a new decision.
        if approval.status != ApprovalStatus.PENDING:
            raise app_error(
                "APPROVAL_ALREADY_DECIDED",
                http_status=409,
                safe_message="审批已决定，不能重复决定",
                details={"status": approval.status.value, "approval_id": str(approval.id)},
            )
        # Optimistic lock guards concurrent editors.
        if approval.version != expected_version:
            raise app_error(
                "VERSION_CONFLICT",
                http_status=409,
                safe_message="审批版本冲突，请刷新后重试",
                details={
                    "expected_version": expected_version,
                    "current_version": approval.version,
                },
            )

        final_params = self._resolve_final_params(approval, decision, edited_params)
        new_status = _decision_to_status(decision)
        approval.status = new_status
        approval.final_params_json = final_params
        approval.decided_by = actor.user_id
        approval.decided_at = now
        approval.version += 1
        await self._approval_repo.save_approval(approval)
        await self._run_service.emit_status(
            agent_run,
            status=agent_run.status,
            message_key="approval.decided",
            safe_payload={
                "approval_id": str(approval.id),
                "decision": decision.value,
                "status": new_status.value,
            },
        )

        if new_status == ApprovalStatus.REJECTED:
            # No side effect; the run completes and the slot is cleared upstream.
            application_run.completion_reason = "ACTION_REJECTED"
            await self._arun_repo.save_application_run(application_run)
            await self._run_service.complete_run(
                agent_run, reason="ACTION_REJECTED", message_key="run.rejected"
            )
        else:
            # APPROVED/EDITED: resume the graph from its checkpoint (upstream).
            await self._run_service.emit_status(
                agent_run,
                status=RunStatus.RUNNING,
                message_key="run.resumed_after_approval",
                safe_payload={"approval_id": str(approval.id)},
            )
        return approval, new_status

    @staticmethod
    def _resolve_final_params(
        approval: Approval, decision: DecisionAction, edited_params: dict[str, Any] | None
    ) -> dict[str, Any]:
        if decision == DecisionAction.EDIT:
            if not edited_params or not isinstance(edited_params.get("target_status"), str):
                raise app_error(
                    "APPROVAL_INVALID_EDIT",
                    http_status=422,
                    safe_message="编辑审批必须提供 target_status",
                )
            # Business IDs are never editable; only the target status is carried.
            return {"target_status": str(edited_params["target_status"])}
        return approval.original_params_json or {}
