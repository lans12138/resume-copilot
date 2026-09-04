"""Approval timeout sweep service (IMP-023).

``MaintenanceService.expire_pending_approvals`` is the business core behind the
``maintenance.expire_approvals`` Celery task (detailed design §11.8). For every
``PENDING`` approval whose ``expires_at`` is at or before ``before``, it performs
the per-record transition in the canonical lock order — JobApplication → Run →
Approval (§5.6) — and only then writes ``EXPIRED/TIMEOUT``, fails the run as
``retryable=True`` with ``error_code=APPROVAL_EXPIRED``, clears the active slot,
and writes the run-failed event + audit.

Concurrency contract: the approval is re-fetched *last* and its status re-checked
before any write. If a concurrent ``decide`` (or another sweep batch) already moved
it off ``PENDING``, this record is skipped — guaranteeing a single terminal
transition and mirroring the "decide exactly once" guard on the decision path.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from backend.app.agent.service import RunService
from backend.app.approvals.models import Approval, ApprovalStatus
from backend.app.approvals.repository import ApprovalRepository
from backend.app.approvals.service import ApprovalService
from backend.app.job_applications.repository import (
    ApplicationRunRepository,
    JobApplicationRepository,
)

# Stable error code written to the run when its approval times out (§11.8).
APPROVAL_EXPIRED_ERROR_CODE = "APPROVAL_EXPIRED"


class MaintenanceService:
    """Scheduled maintenance transitions over approvals and runs."""

    def __init__(
        self,
        *,
        approval_repo: ApprovalRepository,
        arun_repo: ApplicationRunRepository,
        app_repo: JobApplicationRepository,
        run_service: RunService,
        approval_service: ApprovalService,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._approval_repo = approval_repo
        self._arun_repo = arun_repo
        self._app_repo = app_repo
        self._run_service = run_service
        self._approval_service = approval_service
        self._now = now or (lambda: datetime.now(tz=datetime.now().astimezone().tzinfo))

    async def expire_pending_approvals(self, before: datetime) -> int:
        """Promote every timed-out PENDING approval to EXPIRED; return count.

        ``before`` is the cutoff (typically ``transaction_timestamp()`` in the SQL
        path). Each candidate is evaluated independently so one bad record cannot
        abort the batch.
        """
        candidates = await self._approval_repo.get_expired_pending(before)
        expired = 0
        for approval in candidates:
            if await self._expire_one_timeout(approval):
                expired += 1
        return expired

    async def _expire_one_timeout(self, approval: Approval) -> bool:
        """Transition one approval to EXPIRED/TIMEOUT (idempotent under races).

        Returns True if this call performed the transition, False if the record was
        already gone or no longer PENDING (so a concurrent decision/sweep won).
        """
        # Lock order (§5.6): JobApplication -> Run -> Approval. Re-fetching in this
        # order lets the SQL adapter row-lock each aggregate before the next read.
        application_run = await self._arun_repo.get_application_run(approval.application_run_id)
        if application_run is None:
            return False
        application = await self._app_repo.get_application(application_run.application_id)
        if application is None:
            return False
        agent_run = await self._run_service.get_run(approval.application_run_id)
        if agent_run is None:
            return False

        # Re-fetch the approval last and re-check PENDING: the decisive guard.
        current = await self._approval_repo.get_approval(approval.id)
        if current is None or current.status != ApprovalStatus.PENDING:
            return False

        # 1. Approval -> EXPIRED/TIMEOUT (idempotent; no-op if already moved).
        await self._approval_service.mark_expired(current.id, reason="TIMEOUT")
        # 2. Run -> FAILED, retryable so a new attempt can reclaim the slot (§11.8).
        await self._run_service.mark_failed(
            agent_run,
            reason="APPROVAL_EXPIRED",
            retryable=True,
            error_code=APPROVAL_EXPIRED_ERROR_CODE,
            failed_node="human_review",
        )
        # 3. Clear the exclusive slot so the retry can claim it again (§11.8 step 5).
        await self._app_repo.clear_active_run(application.id, agent_run.id)
        return True
