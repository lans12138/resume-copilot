"""Maintenance coordination services (IMP-023 + FIN-006).

Three scheduled jobs live here, all of them *reconciliation* rather than primary
execution. Each takes a durable fact some other component left behind — a
timed-out approval, a committed-but-unpublished row, a file with no database
reference — and moves it to where the system would have put it had nothing
failed. None of them invent business outcomes.

``expire_pending_approvals`` (IMP-023)
    Promotes timed-out ``PENDING`` approvals to ``EXPIRED/TIMEOUT`` (§11.8).

``republish_queued`` (FIN-006)
    Re-delivers work that was committed but whose publication never landed: the
    Redis outage described in §14.4 leaves a row in ``QUEUED``/``CREATED`` with
    no task in flight, and nothing else would ever pick it up.

``cleanup_orphan_files`` (FIN-006)
    Deletes stored objects that no database row references and that are older
    than a safety window, so an interrupted upload cannot accumulate forever.

Shared design point — *why these are safe to re-run*: every transition is
guarded by a re-read of the durable state immediately before it. A batch
delivered twice, or two workers sweeping at once, converge on the same result
instead of double-applying it. The maintenance tasks are at-least-once like
every other Celery task, and nothing here weakens that assumption.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from backend.app.agent.enqueuer import ExecutionIntent, RunEnqueuer
from backend.app.agent.models import RunStatus, RunType
from backend.app.agent.repository import AgentRunRepository
from backend.app.agent.service import RunService
from backend.app.approvals.models import Approval, ApprovalStatus
from backend.app.approvals.repository import ApprovalRepository
from backend.app.approvals.service import ApprovalService
from backend.app.documents.models import ResumeDocument
from backend.app.job_applications.repository import (
    ApplicationRunRepository,
    JobApplicationRepository,
)
from backend.app.maintenance.repository import (
    MaintenanceDocumentRepository,
    OrphanFileTarget,
    StoredObjectLister,
)

logger = logging.getLogger(__name__)

# Stable error code written to the run when its approval times out (§11.8).
APPROVAL_EXPIRED_ERROR_CODE = "APPROVAL_EXPIRED"


class ApplicationRunRecoveryProbe(Protocol):
    """Reads the durable facts that authorise resuming a paused ApplicationRun.

    A narrow port because the republish scan has to answer one question the run
    header cannot: *is this paused run actually resumable?* A run in ``RUNNING``
    with no checkpoint, or with no approval that ever reached ``EXECUTED``, must
    not be re-driven. Only the checkpoint store and the approval store can say.
    """

    async def latest_checkpoint_version(self, thread_id: str) -> str | None:
        """The durable checkpoint id a resume would continue from, if any."""
        ...

    async def executed_approval_for_run(self, run_id: UUID) -> Approval | None:
        """The most recent approval on ``run_id`` that reached ``EXECUTED``."""
        ...


@dataclass(frozen=True, slots=True)
class RepublishOutcome:
    """What one republish sweep did, for logs and tests."""

    documents: int = 0
    match_runs: int = 0
    application_runs: int = 0
    skipped: tuple[str, ...] = field(default=())

    @property
    def total(self) -> int:
        return self.documents + self.match_runs + self.application_runs


@dataclass(frozen=True, slots=True)
class OrphanCleanupOutcome:
    """Deletion counts for one orphan sweep."""

    scanned: int = 0
    deleted: int = 0
    skipped_referenced: int = 0
    skipped_recent: int = 0
    skipped_malformed: int = 0


class MaintenanceService:
    """Scheduled reconciliation over approvals, runs, documents, and storage."""

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

    # ------------------------------------------------------------------
    # IMP-023: Approval timeout sweep
    # ------------------------------------------------------------------

    async def expire_pending_approvals(self, before: datetime, *, batch_size: int = 100) -> int:
        """Promote timed-out PENDING approvals to EXPIRED; return how many moved.

        ``before`` is the cutoff. Candidates are claimed in stable ``id`` order
        with ``FOR UPDATE SKIP LOCKED`` (§11.8), so two sweepers draining the same
        backlog neither block on each other nor expire the same record twice.
        Each record is then evaluated independently: one bad row cannot abort the
        batch.
        """
        candidates = await self._approval_repo.claim_expired_pending(before, limit=batch_size)
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

    # ------------------------------------------------------------------
    # FIN-006: republish scan (§14.4)
    # ------------------------------------------------------------------

    async def republish_queued(
        self,
        *,
        documents: MaintenanceDocumentRepository,
        runs: AgentRunRepository,
        enqueuer: RunEnqueuer,
        document_enqueue: Callable[[UUID, int], None],
        parser_version: str,
        queued_grace: timedelta,
        run_lease: timedelta,
        recovery_probe: ApplicationRunRecoveryProbe | None = None,
        batch_size: int = 100,
    ) -> RepublishOutcome:
        """Re-deliver work whose publication never landed (§14.4, FIN-006).

        Three backlogs, each with its own "is this really stranded?" test, because
        the three have genuinely different failure shapes:

        1. **Documents** in ``QUEUED`` — the upload committed and the parse task
           was published, but Redis dropped it. Re-publishing is idempotent: the
           parse service guards on ``attempt``/``parser_version`` (IMP-012), so a
           duplicate delivery is a no-op rather than a second parse.

        2. **MatchRuns** in ``CREATED`` — the run header committed, the task never
           arrived. Safe because ``CREATED`` means *no worker ever started*: the
           claim is what moves a run out of ``CREATED`` and it commits that
           transition once, at the end, so a killed worker rolls back to
           ``CREATED``. ``START`` is the only intent that accepts ``CREATED``
           (:func:`backend.app.agent.tasks.decide_claim`).

        3. **ApplicationRuns** in ``RUNNING`` that have gone quiet — *not*
           obviously stranded, because ``RUNNING`` also means "a live worker may
           hold the row lock right now". Re-delivering one of those would run the
           graph twice, so this only publishes a ``RESUME`` when the run has been
           quiet for the whole lease window *and* the durable facts of a genuine
           resume exist: a checkpoint to continue from and an ``EXECUTED``
           approval that authorised it. The claim re-validates all of it
           (checkpoint id, approval id, decider, live authorization), so this scan
           can only *propose* a delivery — it can never itself push a run through
           the human gate.
        """
        now = self._now()
        document_count = await self._republish_documents(
            documents=documents,
            enqueue=document_enqueue,
            queued_before=now - queued_grace,
            parser_version=parser_version,
            batch_size=batch_size,
        )
        match_count = await self._republish_match_runs(
            runs=runs,
            enqueuer=enqueuer,
            created_before=now - run_lease,
            batch_size=batch_size,
        )
        application_count, skipped = await self._republish_application_runs(
            runs=runs,
            enqueuer=enqueuer,
            probe=recovery_probe,
            quiet_before=now - run_lease,
            batch_size=batch_size,
        )
        return RepublishOutcome(
            documents=document_count,
            match_runs=match_count,
            application_runs=application_count,
            skipped=tuple(skipped),
        )

    async def _republish_documents(
        self,
        *,
        documents: MaintenanceDocumentRepository,
        enqueue: Callable[[UUID, int], None],
        queued_before: datetime,
        parser_version: str,
        batch_size: int,
    ) -> int:
        stranded: list[ResumeDocument] = await documents.list_stranded_parses(
            queued_before=queued_before, limit=batch_size
        )
        for document in stranded:
            # One enqueue failure must not abandon the rest of the backlog; the
            # next sweep (or the retry endpoint) will try again.
            try:
                enqueue(document.id, document.attempt)
            except Exception:  # noqa: BLE001 - broker boundary, retried next sweep
                logger.exception(
                    "maintenance.republish_queued document enqueue failed id=%s", document.id
                )
        return len(stranded)

    async def _republish_match_runs(
        self,
        *,
        runs: AgentRunRepository,
        enqueuer: RunEnqueuer,
        created_before: datetime,
        batch_size: int,
    ) -> int:
        stranded = await runs.list_stale_runs(
            run_type=RunType.MATCH,
            statuses=(RunStatus.CREATED,),
            older_than=created_before,
            limit=batch_size,
        )
        published = 0
        for run in stranded:
            try:
                enqueuer.enqueue_match_run(
                    run.id, attempt=run.attempt, intent=ExecutionIntent.START
                )
            except Exception:  # noqa: BLE001 - broker boundary, retried next sweep
                logger.exception(
                    "maintenance.republish_queued match run enqueue failed id=%s", run.id
                )
                continue
            published += 1
        return published

    async def _republish_application_runs(
        self,
        *,
        runs: AgentRunRepository,
        enqueuer: RunEnqueuer,
        probe: ApplicationRunRecoveryProbe | None,
        quiet_before: datetime,
        batch_size: int,
    ) -> tuple[int, list[str]]:
        """Re-deliver genuinely resumable quiet runs; return (count, skip reasons)."""
        if probe is None:
            return 0, []
        quiet = await runs.list_stale_runs(
            run_type=RunType.APPLICATION,
            statuses=(RunStatus.RUNNING,),
            older_than=quiet_before,
            limit=batch_size,
        )
        published = 0
        skipped: list[str] = []
        for run in quiet:
            resume_version = await probe.latest_checkpoint_version(run.thread_id)
            approval = await probe.executed_approval_for_run(run.id)
            if resume_version is None or approval is None:
                # Nothing to resume from. A run with no checkpoint never reached a
                # gate, and one with no executed approval was never authorised;
                # either way a human must decide what happens next.
                skipped.append(f"{run.id}:no_resume_facts")
                continue
            decider = approval.decided_by
            if decider is None:
                # EXECUTED implies decided_by is set by the decision path, so a
                # null here means the row is inconsistent. Do not guess an actor.
                skipped.append(f"{run.id}:approval_decider_missing")
                continue
            try:
                enqueuer.enqueue_application_run(
                    run.id,
                    attempt=run.attempt,
                    intent=ExecutionIntent.RESUME,
                    actor_id=decider,
                    resume_version=resume_version,
                    approval_id=approval.id,
                )
            except Exception:  # noqa: BLE001 - broker boundary, retried next sweep
                logger.exception(
                    "maintenance.republish_queued application run enqueue failed id=%s", run.id
                )
                continue
            published += 1
        return published, skipped

    # ------------------------------------------------------------------
    # FIN-006: orphan object cleanup
    # ------------------------------------------------------------------

    async def cleanup_orphan_files(
        self,
        *,
        storage: StoredObjectLister,
        documents: MaintenanceDocumentRepository,
        safety_window: timedelta,
        batch_size: int = 500,
    ) -> OrphanCleanupOutcome:
        """Delete stored objects with no database reference and past the window.

        The order is deliberate and not negotiable: **list → filter by age →
        check reference → delete**. Checking the reference immediately before the
        delete is what makes an interrupted upload safe — an object is protected
        for the whole ``safety_window``, and a reference committed at any point is
        seen by the check that runs after the listing. An object that *is*
        referenced is left alone regardless of age, which is why this can never
        delete a live resume.

        A malformed or unreadable entry is skipped rather than guessed at: an
        object this code cannot address is not this code's to delete.
        """
        cutoff = self._now() - safety_window
        candidates: list[OrphanFileTarget] = await storage.list_objects(limit=batch_size)
        deleted = 0
        referenced = 0
        recent = 0
        malformed = 0
        for target in candidates:
            if target.storage_key is None:
                malformed += 1
                continue
            if target.modified_at is not None and target.modified_at > cutoff:
                recent += 1
                continue
            if await documents.is_storage_key_referenced(target.storage_key):
                referenced += 1
                continue
            # Re-check the age right before deleting: the listing may be slow, but
            # an object must never be deleted while inside its safety window.
            if target.modified_at is not None and target.modified_at > self._now() - safety_window:
                recent += 1
                continue
            await storage.delete_object(target.storage_key)
            deleted += 1
        return OrphanCleanupOutcome(
            scanned=len(candidates),
            deleted=deleted,
            skipped_referenced=referenced,
            skipped_recent=recent,
            skipped_malformed=malformed,
        )
