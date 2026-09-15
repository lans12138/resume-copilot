"""FIN-006: maintenance coordination — republish scan, orphan cleanup, races.

Three concerns are pinned here, each against the failure it exists to survive:

**Republish (§14.4).** Redis can drop a publication and Celery delivers at least
once, so a row can be committed with no task in flight. The sweep must find
exactly the stranded work and *only* the stranded work — publishing a run whose
worker is alive would execute the graph twice, and publishing a run sitting at a
human gate would bypass the approval.

**Orphan cleanup.** A stored object with no referencing row and past the safety
window is deleted; a referenced object, a young object, and an object outside the
managed layout are not. The last two matter most: an interrupted upload must
survive, and a file this code cannot address must not be guessed at.

**Races and re-runs.** A batch delivered twice, or two sweepers at once, must
converge. The expiry sweep uses stable ordering + ``SKIP LOCKED`` so concurrent
workers do not overlap, and every transition re-checks durable state first.

Two structural notes on the tests themselves:

* Where the assertion is "a lock was taken", the test inspects the emitted SQL
  (``FOR UPDATE SKIP LOCKED`` is a database behaviour an in-memory adapter cannot
  demonstrate). Where the assertion is "the outcome converges", the test drives
  the service layer directly and stays hermetic.
* ``test_republish_application_run_never_proposes_an_unapproved_run`` is the
  negative control for the whole feature: it is the case where doing nothing is
  the correct answer, and a republish scan that "helps" here is a security bug.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql

from backend.app.agent.checkpoint import InMemoryCheckpointer
from backend.app.agent.enqueuer import ExecutionIntent
from backend.app.agent.models import AgentRun, RunStatus, RunType
from backend.app.agent.repository import InMemoryAgentRunRepository
from backend.app.agent.service import RunService
from backend.app.approvals.models import Approval, ApprovalStatus
from backend.app.approvals.repository import InMemoryApprovalRepository, SqlApprovalRepository
from backend.app.approvals.service import ApprovalService
from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.documents.models import DocumentStatus, ResumeDocument
from backend.app.job_applications.models import ApplicationRun, ApplicationStatus, JobApplication
from backend.app.job_applications.repository import (
    InMemoryApplicationRunRepository,
    InMemoryJobApplicationRepository,
)
from backend.app.job_applications.service import ApplicationRunService
from backend.app.maintenance.repository import (
    InMemoryMaintenanceDocumentRepository,
    LocalVolumeStorageLister,
    OrphanFileTarget,
)
from backend.app.maintenance.service import MaintenanceService

FIXED_NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
LEASE = timedelta(minutes=15)
GRACE = timedelta(minutes=2)


# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------


class RecordingRunEnqueuer:
    """Captures the deliveries a sweep proposes, without a broker."""

    def __init__(self) -> None:
        self.match_runs: list[tuple[UUID, int, ExecutionIntent]] = []
        self.application_runs: list[tuple[UUID, int, ExecutionIntent, UUID, str, UUID]] = []

    def enqueue_match_run(self, run_id: UUID, *, attempt: int, intent: ExecutionIntent) -> None:
        self.match_runs.append((run_id, attempt, intent))

    def enqueue_application_run(
        self,
        run_id: UUID,
        *,
        attempt: int,
        intent: ExecutionIntent,
        actor_id: UUID,
        resume_version: str | None = None,
        approval_id: UUID | None = None,
    ) -> None:
        assert resume_version is not None and approval_id is not None
        self.application_runs.append(
            (run_id, attempt, intent, actor_id, resume_version, approval_id)
        )


class FailFirstMatchEnqueuer(RecordingRunEnqueuer):
    """Fails the first match-run publication, to model a broker outage."""

    def __init__(self, failing_run_id: UUID) -> None:
        super().__init__()
        self._failing_run_id = failing_run_id

    def enqueue_match_run(self, run_id: UUID, *, attempt: int, intent: ExecutionIntent) -> None:
        if run_id == self._failing_run_id:
            raise RuntimeError("redis unavailable")
        super().enqueue_match_run(run_id, attempt=attempt, intent=intent)


class StaticRecoveryProbe:
    """Reports fixed resume facts so the sweep's own logic is under test."""

    def __init__(self, *, checkpoint_version: str | None, approval: Approval | None) -> None:
        self._checkpoint_version = checkpoint_version
        self._approval = approval

    async def latest_checkpoint_version(self, thread_id: str) -> str | None:
        return self._checkpoint_version

    async def executed_approval_for_run(self, run_id: UUID) -> Approval | None:
        return self._approval


class FakeObjectStore:
    """In-memory ``StoredObjectLister`` recording deletions."""

    def __init__(self, targets: list[OrphanFileTarget]) -> None:
        self.targets = targets
        self.deleted: list[str] = []

    async def list_objects(self, *, limit: int) -> list[OrphanFileTarget]:
        return self.targets[:limit]

    async def delete_object(self, storage_key: str) -> None:
        self.deleted.append(storage_key)


class CapturingSession:
    """Minimal session stand-in that records statements instead of executing them."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> CapturingResult:
        self.statements.append(statement)
        return CapturingResult()

    def compiled_sql(self, index: int) -> str:
        """Render the recorded statement's SQL for the PostgreSQL dialect."""

        def render(statement: Any) -> str:
            return str(
                statement.compile(
                    # ``dialect()`` is untyped in SQLAlchemy's stubs.
                    dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
                    compile_kwargs={"literal_binds": True},
                )
            )

        return render(self.statements[index])


class CapturingResult:
    def scalars(self) -> CapturingResult:
        return self

    def all(self) -> list[Any]:
        return []


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------


class Core:
    """One maintenance test harness; every dependency shares its in-memory store."""

    def __init__(self, *, clock: datetime = FIXED_NOW) -> None:
        self.agent_repo = InMemoryAgentRunRepository()
        self.app_repo = InMemoryJobApplicationRepository()
        self.arun_repo = InMemoryApplicationRunRepository()
        self.approval_repo = InMemoryApprovalRepository()
        self.run_service = RunService(self.agent_repo, InMemoryCheckpointer())

        def now() -> datetime:
            return clock

        async def authorize(actor: Actor, job_id: UUID) -> None:
            return None

        self.approval_service = ApprovalService(
            authorize=authorize,
            approval_repo=self.approval_repo,
            arun_repo=self.arun_repo,
            app_repo=self.app_repo,
            run_service=self.run_service,
            now=now,
        )
        self.application_run_service = ApplicationRunService(
            app_repo=self.app_repo,
            arun_repo=self.arun_repo,
            run_service=self.run_service,
            authorize=authorize,
            approval_service=self.approval_service,
        )
        self.maintenance = MaintenanceService(
            approval_repo=self.approval_repo,
            arun_repo=self.arun_repo,
            app_repo=self.app_repo,
            run_service=self.run_service,
            approval_service=self.approval_service,
            now=now,
        )

    async def republish(
        self,
        *,
        enqueuer: RecordingRunEnqueuer,
        documents: InMemoryMaintenanceDocumentRepository | None = None,
        documents_published: list[tuple[UUID, int]] | None = None,
        recovery_probe: StaticRecoveryProbe | None = None,
        batch_size: int = 100,
    ) -> Any:
        sink = documents_published if documents_published is not None else []

        def enqueue_document(document_id: UUID, attempt: int) -> None:
            sink.append((document_id, attempt))

        return await self.maintenance.republish_queued(
            documents=documents or InMemoryMaintenanceDocumentRepository(),
            runs=self.agent_repo,
            enqueuer=enqueuer,
            document_enqueue=enqueue_document,
            parser_version="v1",
            queued_grace=GRACE,
            run_lease=LEASE,
            recovery_probe=recovery_probe,
            batch_size=batch_size,
        )

    async def create_pending_run(self) -> tuple[AgentRun, ApplicationRun, UUID]:
        """Create an application + run parked at the first approval gate."""
        application = JobApplication(
            id=uuid4(),
            job_id=uuid4(),
            candidate_id=uuid4(),
            status=ApplicationStatus.CREATED,
            version=1,
        )
        await self.app_repo.save_application(application)
        agent_run, application_run = await self.application_run_service.create_application_run(
            _actor(), application.id, None
        )
        return agent_run, application_run, application.id


def _actor() -> Actor:
    return Actor(user_id=uuid4(), username="tester", role=UserRole.HIRING_MANAGER)


def _match_run(
    *,
    status: RunStatus,
    updated_at: datetime,
    attempt: int = 1,
) -> AgentRun:
    return AgentRun(
        id=uuid4(),
        thread_id=f"thread-{uuid4()}",
        run_type=RunType.MATCH,
        status=status,
        attempt=attempt,
        config_snapshot_json={},
        updated_at=updated_at,
    )


def _application_run(
    *,
    status: RunStatus,
    updated_at: datetime,
    attempt: int = 2,
) -> AgentRun:
    return AgentRun(
        id=uuid4(),
        thread_id=f"thread-{uuid4()}",
        run_type=RunType.APPLICATION,
        status=status,
        attempt=attempt,
        config_snapshot_json={},
        updated_at=updated_at,
    )


def _approval(run_id: UUID, *, status: ApprovalStatus, decided_by: UUID | None) -> Approval:
    return Approval(
        id=uuid4(),
        application_run_id=run_id,
        status=status,
        idempotency_key=f"{run_id}:2:UPDATE_APPLICATION_STATUS:1",
        version=1,
        decided_by=decided_by,
        executed_at=FIXED_NOW if status is ApprovalStatus.EXECUTED else None,
    )


def _document(
    *,
    status: DocumentStatus,
    updated_at: datetime,
    attempt: int = 0,
    storage_key: str | None = None,
) -> ResumeDocument:
    return ResumeDocument(
        id=uuid4(),
        original_filename="resume.pdf",
        storage_key=storage_key or f"objects/{uuid4().hex[:2]}/{uuid4().hex}",
        media_type="application/pdf",
        size_bytes=1024,
        content_sha256=uuid4().hex + uuid4().hex,
        status=status,
        attempt=attempt,
        uploaded_by=uuid4(),
        updated_at=updated_at,
    )


def _orphan_key() -> str:
    return f"objects/{uuid4().hex[:2]}/{uuid4().hex}"


# ----------------------------------------------------------------------
# Republish: documents
# ----------------------------------------------------------------------


def test_republish_publishes_only_stranded_documents() -> None:
    """A QUEUED document past the grace window is republished; others are not."""

    async def _run() -> None:
        core = Core()
        stranded = _document(
            status=DocumentStatus.QUEUED, updated_at=FIXED_NOW - timedelta(hours=1)
        )
        fresh = _document(
            status=DocumentStatus.QUEUED, updated_at=FIXED_NOW - timedelta(seconds=1)
        )
        parsing = _document(
            status=DocumentStatus.PARSING, updated_at=FIXED_NOW - timedelta(hours=1)
        )
        documents = InMemoryMaintenanceDocumentRepository(
            {item.id: item for item in (stranded, fresh, parsing)}
        )
        published: list[tuple[UUID, int]] = []
        enqueuer = RecordingRunEnqueuer()

        outcome = await core.republish(
            enqueuer=enqueuer, documents=documents, documents_published=published
        )

        assert outcome.documents == 1
        assert published == [(stranded.id, stranded.attempt)]
        assert enqueuer.match_runs == []

    asyncio.run(_run())


# ----------------------------------------------------------------------
# Republish: MatchRun
# ----------------------------------------------------------------------


def test_republish_match_runs_uses_start_intent_for_created() -> None:
    """``CREATED`` is republished with ``START`` — the only intent that accepts it."""

    async def _run() -> None:
        core = Core()
        stale = _match_run(status=RunStatus.CREATED, updated_at=FIXED_NOW - timedelta(hours=1))
        recent = _match_run(status=RunStatus.CREATED, updated_at=FIXED_NOW - timedelta(minutes=1))
        running = _match_run(status=RunStatus.RUNNING, updated_at=FIXED_NOW - timedelta(hours=1))
        for run in (stale, recent, running):
            await core.agent_repo.save_run(run)
        enqueuer = RecordingRunEnqueuer()

        outcome = await core.republish(enqueuer=enqueuer)

        assert outcome.match_runs == 1
        assert enqueuer.match_runs == [(stale.id, stale.attempt, ExecutionIntent.START)]

    asyncio.run(_run())


def test_republish_match_run_never_republishes_waiting_approval() -> None:
    """A run parked at the human gate is never re-driven by the sweep.

    The republish scan only looks at ``CREATED``, so ``WAITING_APPROVAL`` is
    excluded by construction — which is what keeps "未审批副作用执行次数必须为 0"
    true even while the sweep runs.
    """

    async def _run() -> None:
        core = Core()
        gated = _match_run(
            status=RunStatus.WAITING_APPROVAL, updated_at=FIXED_NOW - timedelta(days=1)
        )
        await core.agent_repo.save_run(gated)
        enqueuer = RecordingRunEnqueuer()

        outcome = await core.republish(enqueuer=enqueuer)

        assert outcome.match_runs == 0
        assert enqueuer.match_runs == []

    asyncio.run(_run())


def test_republish_match_run_skips_terminal_states() -> None:
    """Terminal runs are never candidates, however quiet they are."""

    async def _run() -> None:
        core = Core()
        for status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
            await core.agent_repo.save_run(
                _match_run(status=status, updated_at=FIXED_NOW - timedelta(days=30))
            )
        enqueuer = RecordingRunEnqueuer()

        outcome = await core.republish(enqueuer=enqueuer)

        assert outcome.match_runs == 0

    asyncio.run(_run())


# ----------------------------------------------------------------------
# Republish: ApplicationRun resume (the narrow, dangerous case)
# ----------------------------------------------------------------------


def test_republish_application_run_resumes_with_durable_facts() -> None:
    """A quiet RUNNING run with a checkpoint + EXECUTED approval is re-delivered."""

    async def _run() -> None:
        core = Core()
        decider = uuid4()
        quiet = _application_run(
            status=RunStatus.RUNNING, updated_at=FIXED_NOW - timedelta(hours=1)
        )
        await core.agent_repo.save_run(quiet)
        approval = _approval(quiet.id, status=ApprovalStatus.EXECUTED, decided_by=decider)
        enqueuer = RecordingRunEnqueuer()

        outcome = await core.republish(
            enqueuer=enqueuer,
            recovery_probe=StaticRecoveryProbe(
                checkpoint_version="ckpt-7", approval=approval
            ),
        )

        assert outcome.application_runs == 1
        assert enqueuer.application_runs == [
            (quiet.id, quiet.attempt, ExecutionIntent.RESUME, decider, "ckpt-7", approval.id)
        ]

    asyncio.run(_run())


def test_republish_application_run_never_proposes_an_unapproved_run() -> None:
    """Negative control: with no EXECUTED approval the sweep stays out of the way.

    This is the case where *doing nothing* is the correct answer. A republish scan
    that "helpfully" resumed here would push a run through the human gate, so this
    assertion is a security property rather than a nicety.
    """

    async def _run() -> None:
        core = Core()
        quiet = _application_run(
            status=RunStatus.RUNNING, updated_at=FIXED_NOW - timedelta(hours=1)
        )
        await core.agent_repo.save_run(quiet)
        enqueuer = RecordingRunEnqueuer()

        outcome = await core.republish(
            enqueuer=enqueuer,
            recovery_probe=StaticRecoveryProbe(checkpoint_version=None, approval=None),
        )

        assert outcome.application_runs == 0
        assert enqueuer.application_runs == []
        assert any("no_resume_facts" in reason for reason in outcome.skipped)

    asyncio.run(_run())


def test_republish_application_run_skips_missing_decider() -> None:
    """An EXECUTED approval with no decider is inconsistent; do not invent one."""

    async def _run() -> None:
        core = Core()
        quiet = _application_run(
            status=RunStatus.RUNNING, updated_at=FIXED_NOW - timedelta(hours=1)
        )
        await core.agent_repo.save_run(quiet)
        approval = _approval(quiet.id, status=ApprovalStatus.EXECUTED, decided_by=None)
        enqueuer = RecordingRunEnqueuer()

        outcome = await core.republish(
            enqueuer=enqueuer,
            recovery_probe=StaticRecoveryProbe(
                checkpoint_version="ckpt-7", approval=approval
            ),
        )

        assert outcome.application_runs == 0
        assert enqueuer.application_runs == []
        assert any("approval_decider_missing" in reason for reason in outcome.skipped)

    asyncio.run(_run())


def test_republish_application_run_waits_out_the_lease() -> None:
    """A run touched inside the lease window is treated as live, not abandoned."""

    async def _run() -> None:
        core = Core()
        live = _application_run(
            status=RunStatus.RUNNING, updated_at=FIXED_NOW - timedelta(minutes=1)
        )
        await core.agent_repo.save_run(live)
        approval = _approval(live.id, status=ApprovalStatus.EXECUTED, decided_by=uuid4())
        enqueuer = RecordingRunEnqueuer()

        outcome = await core.republish(
            enqueuer=enqueuer,
            recovery_probe=StaticRecoveryProbe(
                checkpoint_version="ckpt-7", approval=approval
            ),
        )

        assert outcome.application_runs == 0
        assert enqueuer.application_runs == []

    asyncio.run(_run())


def test_republish_without_probe_never_touches_application_runs() -> None:
    """No probe means no resume facts, so the sweep declines to guess."""

    async def _run() -> None:
        core = Core()
        quiet = _application_run(
            status=RunStatus.RUNNING, updated_at=FIXED_NOW - timedelta(days=1)
        )
        await core.agent_repo.save_run(quiet)
        enqueuer = RecordingRunEnqueuer()

        outcome = await core.republish(enqueuer=enqueuer)

        assert outcome.application_runs == 0

    asyncio.run(_run())


def test_republish_is_idempotent_across_repeated_sweeps() -> None:
    """Running the sweep twice proposes the same deliveries — never more.

    Publishing is not the guarantee (the claim is), but a sweep that *multiplied*
    its publications each cycle would hammer the broker during an outage. The
    candidate set comes from durable state, so a second pass sees the same rows
    and proposes the same work.
    """

    async def _run() -> None:
        core = Core()
        stale = _match_run(status=RunStatus.CREATED, updated_at=FIXED_NOW - timedelta(hours=2))
        await core.agent_repo.save_run(stale)
        document = _document(
            status=DocumentStatus.QUEUED, updated_at=FIXED_NOW - timedelta(hours=2)
        )
        documents = InMemoryMaintenanceDocumentRepository({document.id: document})
        enqueuer = RecordingRunEnqueuer()
        published: list[tuple[UUID, int]] = []

        await core.republish(
            enqueuer=enqueuer, documents=documents, documents_published=published
        )
        await core.republish(
            enqueuer=enqueuer, documents=documents, documents_published=published
        )

        assert enqueuer.match_runs == [
            (stale.id, stale.attempt, ExecutionIntent.START),
            (stale.id, stale.attempt, ExecutionIntent.START),
        ]
        assert published == [(document.id, document.attempt)] * 2

    asyncio.run(_run())


def test_republish_survives_a_broker_failure_per_record() -> None:
    """One enqueue failure must not abandon the rest of the backlog (§14.2)."""

    async def _run() -> None:
        core = Core()
        # Oldest-first ordering means ``first`` is attempted before ``second``.
        first = _match_run(status=RunStatus.CREATED, updated_at=FIXED_NOW - timedelta(hours=3))
        second = _match_run(status=RunStatus.CREATED, updated_at=FIXED_NOW - timedelta(hours=2))
        await core.agent_repo.save_run(first)
        await core.agent_repo.save_run(second)
        enqueuer = FailFirstMatchEnqueuer(first.id)

        outcome = await core.republish(enqueuer=enqueuer)

        # The failed record is not counted, but the healthy one still went out.
        assert outcome.match_runs == 1
        assert enqueuer.match_runs == [(second.id, second.attempt, ExecutionIntent.START)]

    asyncio.run(_run())


def test_republish_respects_batch_size() -> None:
    """The cap bounds one cycle so a backlog drains steadily, not all at once."""

    async def _run() -> None:
        core = Core()
        for index in range(5):
            await core.agent_repo.save_run(
                _match_run(
                    status=RunStatus.CREATED,
                    updated_at=FIXED_NOW - timedelta(hours=10 - index),
                )
            )
        enqueuer = RecordingRunEnqueuer()

        outcome = await core.republish(enqueuer=enqueuer, batch_size=2)

        assert outcome.match_runs == 2

    asyncio.run(_run())


def test_republish_drains_the_oldest_backlog_first() -> None:
    """Ordering is oldest-first so a long outage does not starve early work."""

    async def _run() -> None:
        core = Core()
        older = _match_run(status=RunStatus.CREATED, updated_at=FIXED_NOW - timedelta(days=2))
        newer = _match_run(status=RunStatus.CREATED, updated_at=FIXED_NOW - timedelta(hours=2))
        # Save in the "wrong" order to prove the ordering comes from the query.
        await core.agent_repo.save_run(newer)
        await core.agent_repo.save_run(older)
        enqueuer = RecordingRunEnqueuer()

        await core.republish(enqueuer=enqueuer, batch_size=1)

        assert enqueuer.match_runs == [(older.id, older.attempt, ExecutionIntent.START)]

    asyncio.run(_run())


# ----------------------------------------------------------------------
# Orphan cleanup
# ----------------------------------------------------------------------


def test_cleanup_deletes_only_unreferenced_aged_objects() -> None:
    """Referenced, recent, and unaddressable entries are all left alone."""

    async def _run() -> None:
        core = Core()
        old_key = _orphan_key()
        young_key = _orphan_key()
        live_key = _orphan_key()
        referenced = _document(
            status=DocumentStatus.READY,
            updated_at=FIXED_NOW - timedelta(days=30),
            storage_key=live_key,
        )
        store = FakeObjectStore(
            [
                OrphanFileTarget(old_key, FIXED_NOW - timedelta(hours=4)),
                OrphanFileTarget(young_key, FIXED_NOW - timedelta(minutes=5)),
                OrphanFileTarget(live_key, FIXED_NOW - timedelta(hours=4)),
                OrphanFileTarget(None, FIXED_NOW - timedelta(hours=4)),
            ]
        )

        outcome = await core.maintenance.cleanup_orphan_files(
            storage=store,
            documents=InMemoryMaintenanceDocumentRepository({referenced.id: referenced}),
            safety_window=timedelta(hours=1),
        )

        assert store.deleted == [old_key]
        assert outcome.scanned == 4
        assert outcome.deleted == 1
        assert outcome.skipped_referenced == 1
        assert outcome.skipped_recent == 1
        assert outcome.skipped_malformed == 1

    asyncio.run(_run())


def test_cleanup_never_deletes_inside_the_safety_window() -> None:
    """Everything younger than the window survives, referenced or not."""

    async def _run() -> None:
        core = Core()
        store = FakeObjectStore(
            [
                OrphanFileTarget(_orphan_key(), FIXED_NOW),
                OrphanFileTarget(_orphan_key(), FIXED_NOW - timedelta(seconds=1)),
            ]
        )

        outcome = await core.maintenance.cleanup_orphan_files(
            storage=store,
            documents=InMemoryMaintenanceDocumentRepository(),
            safety_window=timedelta(hours=1),
        )

        assert store.deleted == []
        assert outcome.deleted == 0
        assert outcome.skipped_recent == 2

    asyncio.run(_run())


def test_cleanup_is_idempotent() -> None:
    """A second sweep over an unchanged store finds nothing new to delete."""

    async def _run() -> None:
        core = Core()
        key = _orphan_key()
        store = FakeObjectStore([OrphanFileTarget(key, FIXED_NOW - timedelta(days=1))])

        first = await core.maintenance.cleanup_orphan_files(
            storage=store,
            documents=InMemoryMaintenanceDocumentRepository(),
            safety_window=timedelta(hours=1),
        )
        # The object is gone, so a real listing would no longer return it; model
        # that by clearing the store's view and sweeping again.
        store.targets = []
        second = await core.maintenance.cleanup_orphan_files(
            storage=store,
            documents=InMemoryMaintenanceDocumentRepository(),
            safety_window=timedelta(hours=1),
        )

        assert first.deleted == 1
        assert second.deleted == 0
        assert store.deleted == [key]

    asyncio.run(_run())


def test_cleanup_keeps_an_object_referenced_by_a_recent_upload() -> None:
    """A just-committed upload protects its object even past the age filter.

    The reference check runs immediately before the delete, so an object whose row
    landed after the listing was taken is still spared.
    """

    async def _run() -> None:
        core = Core()
        key = _orphan_key()
        document = _document(
            status=DocumentStatus.QUEUED,
            updated_at=FIXED_NOW - timedelta(minutes=1),
            storage_key=key,
        )
        store = FakeObjectStore([OrphanFileTarget(key, FIXED_NOW - timedelta(days=2))])

        outcome = await core.maintenance.cleanup_orphan_files(
            storage=store,
            documents=InMemoryMaintenanceDocumentRepository({document.id: document}),
            safety_window=timedelta(hours=1),
        )

        assert store.deleted == []
        assert outcome.skipped_referenced == 1

    asyncio.run(_run())


def test_cleanup_respects_batch_size() -> None:
    """One cycle only ever touches ``batch_size`` objects."""

    async def _run() -> None:
        core = Core()
        keys = [_orphan_key() for _ in range(5)]
        store = FakeObjectStore(
            [OrphanFileTarget(key, FIXED_NOW - timedelta(days=1)) for key in keys]
        )

        outcome = await core.maintenance.cleanup_orphan_files(
            storage=store,
            documents=InMemoryMaintenanceDocumentRepository(),
            safety_window=timedelta(hours=1),
            batch_size=3,
        )

        assert outcome.scanned == 3
        assert outcome.deleted == 3

    asyncio.run(_run())


def test_local_volume_lister_reports_managed_keys_and_ignores_strays(tmp_path: Path) -> None:
    """Real filesystem walk: managed objects get keys, staging files do not."""

    async def _run() -> None:
        objects = tmp_path / "objects" / "ab"
        objects.mkdir(parents=True)
        managed = objects / ("c" * 32)
        managed.write_bytes(b"resume")
        (tmp_path / "objects" / ".tmp").mkdir(parents=True)
        stray = tmp_path / "objects" / ".tmp" / "leftover"
        stray.write_bytes(b"partial")

        lister = LocalVolumeStorageLister(tmp_path)
        targets = await lister.list_objects(limit=100)
        by_key = {target.storage_key: target for target in targets}

        assert f"objects/ab/{'c' * 32}" in by_key
        # The staging file is reported (so it is counted) but carries no key, so
        # the sweep will not try to delete it through the storage API.
        assert None in by_key

        await lister.delete_object(f"objects/ab/{'c' * 32}")
        assert not managed.exists()
        assert stray.exists()

    asyncio.run(_run())


def test_local_volume_lister_refuses_keys_outside_the_root(tmp_path: Path) -> None:
    """A traversal attempt is rejected rather than resolved."""

    async def _run() -> None:
        lister = LocalVolumeStorageLister(tmp_path / "root")
        try:
            await lister.delete_object("../../etc/passwd")
        except ValueError:
            return
        raise AssertionError("expected a ValueError for an escaping key")

    asyncio.run(_run())


def test_local_volume_lister_is_deterministic_and_capped(tmp_path: Path) -> None:
    """Sorted walk + hard cap: a scan cannot walk an unbounded tree."""

    async def _run() -> None:
        for shard in ("aa", "bb"):
            directory = tmp_path / "objects" / shard
            directory.mkdir(parents=True)
            for suffix in ("1" * 32, "2" * 32):
                (directory / suffix).write_bytes(b"x")

        lister = LocalVolumeStorageLister(tmp_path)
        capped = await lister.list_objects(limit=1)
        assert len(capped) == 1
        assert capped[0].storage_key == f"objects/aa/{'1' * 32}"

    asyncio.run(_run())


# ----------------------------------------------------------------------
# Expiry sweep: stable order + SKIP LOCKED
# ----------------------------------------------------------------------


def test_expired_pending_query_uses_stable_order_and_skip_locked() -> None:
    """The batch claim emits ``ORDER BY id LIMIT n FOR UPDATE SKIP LOCKED``.

    This is a database behaviour an in-memory fake cannot demonstrate, so the test
    compiles the emitted SQL for the PostgreSQL dialect and inspects it. Dropping
    either clause reintroduces a real bug: without ``ORDER BY`` two sweepers can
    deadlock on a partially overlapping candidate set, and without ``SKIP LOCKED``
    the second worker blocks instead of draining the backlog.
    """

    async def _run() -> None:
        session = CapturingSession()
        repo = SqlApprovalRepository(session)  # type: ignore[arg-type]
        # ``execute`` is the only session surface the batch claim touches.
        assert await repo.claim_expired_pending(FIXED_NOW, limit=25) == []
        assert len(session.statements) == 1
        sql = session.compiled_sql(0)
        assert "FOR UPDATE SKIP LOCKED" in sql
        assert "ORDER BY approvals.id" in sql
        assert "LIMIT 25" in sql

    asyncio.run(_run())


def test_expire_uses_the_batch_claim_not_an_unbounded_fetch() -> None:
    """The sweeper claims a bounded batch and clears the slot on expiry."""

    async def _run() -> None:
        core = Core()
        agent_run, _app_run, app_id = await core.create_pending_run()
        approval = await core.approval_service.get_pending_by_run(agent_run.id)
        assert approval is not None
        approval.expires_at = FIXED_NOW - timedelta(minutes=1)

        calls: list[int] = []
        original = core.approval_repo.claim_expired_pending

        async def spy(before: datetime, *, limit: int) -> list[Approval]:
            calls.append(limit)
            return await original(before, limit=limit)

        core.approval_repo.claim_expired_pending = spy  # type: ignore[method-assign]
        expired = await core.maintenance.expire_pending_approvals(FIXED_NOW, batch_size=7)

        assert expired == 1
        assert calls == [7]
        refreshed = await core.app_repo.get_application(app_id)
        assert refreshed is not None
        assert refreshed.active_application_run_id is None

    asyncio.run(_run())


def test_fake_claim_expired_pending_honours_the_limit() -> None:
    """The in-memory adapter mirrors the SQL cap so tests cannot pass by accident."""

    async def _run() -> None:
        repo = InMemoryApprovalRepository()
        run_id = uuid4()
        for index in range(5):
            await repo.save_approval(
                Approval(
                    id=uuid4(),
                    application_run_id=run_id,
                    status=ApprovalStatus.PENDING,
                    idempotency_key=f"{run_id}:1:UPDATE_APPLICATION_STATUS:{index}",
                    version=1,
                    expires_at=FIXED_NOW - timedelta(minutes=1),
                )
            )
        batch = await repo.claim_expired_pending(FIXED_NOW, limit=2)
        assert len(batch) == 2
        # Stable ordering (by id) is what the SQL adapter promises too.
        assert [approval.id for approval in batch] == sorted(approval.id for approval in batch)

    asyncio.run(_run())


def test_expire_skips_a_record_a_concurrent_worker_already_took() -> None:
    """``SKIP LOCKED`` only avoids overlap; the re-check is what makes it correct.

    Two sweepers can receive disjoint batches and still both look at one approval
    if the first committed by the time the second re-reads it. The "still PENDING"
    guard is therefore the real protection, and this pins it.
    """

    async def _run() -> None:
        core = Core()
        agent_run, _app_run, app_id = await core.create_pending_run()
        approval = await core.approval_service.get_pending_by_run(agent_run.id)
        assert approval is not None
        approval.expires_at = FIXED_NOW - timedelta(minutes=1)

        # Worker A expires it.
        assert await core.maintenance.expire_pending_approvals(FIXED_NOW) == 1
        # Worker B re-processes the same record (its batch was fetched before A
        # committed); it must be a no-op, not a second transition.
        assert await core.maintenance._expire_one_timeout(approval) is False
        reread = await core.approval_service.get_approval(approval.id)
        assert reread is not None
        assert reread.status is ApprovalStatus.EXPIRED
        refreshed = await core.app_repo.get_application(app_id)
        assert refreshed is not None
        assert refreshed.active_application_run_id is None

    asyncio.run(_run())
