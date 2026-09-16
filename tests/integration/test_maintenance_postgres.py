"""FIN-006: PostgreSQL integration tests for the maintenance sweeps.

``FOR UPDATE SKIP LOCKED``, a partial unique index, and row-level lock ordering
are database behaviours that no in-memory fake can demonstrate, and the plan is
explicit that SQLite is not an acceptable substitute for them. These tests
therefore run against a real PostgreSQL (asyncpg) when ``DATABASE_URL`` points at
one, and are skipped otherwise so the normal unit suite stays hermetic:

    docker run --rm --network resume-copilot_backend --env-file .env.example \
        resume-copilot-backend-development:local \
        pytest -q tests/integration/test_maintenance_postgres.py

Two properties are asserted that only a real database can settle:

* **No two sweepers take the same approval.** Two concurrent
  ``expire_pending_approvals`` calls over one backlog must split it, and each
  approval must be transitioned exactly once (§11.8).
* **The candidate scan reads committed state and is ordered by id.** The batch
  claim must return the lowest-id rows first, which is what keeps two sweepers
  from deadlocking on a partially overlapping set.

Each ``def test_`` drives its async scenario through ``asyncio.run`` (the project's
hermetic convention: no pytest-asyncio plugin is configured).

Two things this suite has to do that a single-table suite does not.

**Seed the FK parents.** These tables are not islands: ``job_applications``
points at ``jobs`` and ``candidates``, ``jobs`` points at ``users``, and
``resume_documents.uploaded_by`` points at ``users``. All of those constraints
are real and non-deferrable, so a random ``uuid4()`` in a parent column is a
foreign-key violation, not a harmless stub.

**Never let two ends of a cycle be pending in one flush.** ``job_applications``
carries a composite foreign key to ``application_runs`` declared with
``use_alter=True`` -- that is what lets the two tables be created at all, since
``application_runs`` points back at ``job_applications``. SQLAlchemy therefore
sees a cycle between the two mappers and breaks it in an order that satisfies one
side and violates the other: put both in a single flush and it emits
``application_runs`` before ``job_applications``, which trips
``fk_application_runs_application``. ``Approval`` makes it worse by having no
``relationship()`` to ``ApplicationRun`` at all -- only a bare foreign key -- so
it carries no dependency and lands wherever the sort puts it.

Production code never hits this because it writes these rows in separate
transactions (the application is created and committed long before a run claims
it). This suite mirrors that: one flush per level of the graph.

The model registry is imported for its side effect: without it only the modules
this file names are on ``Base.metadata``, and every foreign key pointing at a
table nobody imported fails to resolve.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Side-effect import: registers every mapped table on Base.metadata, so the
# foreign keys these seeds depend on resolve.
import backend.app.infrastructure.model_registry  # noqa: F401
from backend.app.agent.models import AgentCheckpoint, AgentRun, RunStatus, RunType
from backend.app.agent.repository import SqlAgentRunRepository
from backend.app.approvals.models import Approval, ApprovalStatus
from backend.app.approvals.repository import SqlApprovalRepository
from backend.app.auth.models import User, UserRole
from backend.app.candidates.models import Candidate
from backend.app.documents.models import DocumentStatus, ResumeDocument
from backend.app.job_applications.models import ApplicationRun, ApplicationStatus, JobApplication
from backend.app.jobs.models import Job, JobStatus
from backend.app.maintenance.repository import (
    SqlApplicationRunRecoveryProbe,
    SqlMaintenanceDocumentRepository,
)

_DATABASE_URL = os.environ.get("DATABASE_URL", "")
_RUN_INTEGRATION = _DATABASE_URL.startswith("postgresql+asyncpg")
pytestmark = pytest.mark.skipif(  # type: ignore[name-defined]
    not _RUN_INTEGRATION, reason="requires DATABASE_URL=postgresql+asyncpg://..."
)

NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)


def _engine_and_factory() -> tuple[object, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _clear(engine: object) -> None:
    """Remove the rows this suite creates, respecting FK order."""
    async with engine.begin() as conn:  # type: ignore[attr-defined]
        await conn.execute(text("DELETE FROM approvals"))
        await conn.execute(text("DELETE FROM application_status_history"))
        await conn.execute(text("DELETE FROM application_runs"))
        await conn.execute(text("DELETE FROM job_applications"))
        await conn.execute(text("DELETE FROM agent_checkpoints"))
        await conn.execute(text("DELETE FROM agent_runs"))
        await conn.execute(text("DELETE FROM resume_documents"))
        await conn.execute(text("DELETE FROM candidates"))
        await conn.execute(text("DELETE FROM jobs"))
        await conn.execute(text("DELETE FROM users"))


async def _seed_context(session: AsyncSession) -> tuple[UUID, UUID]:
    """Insert the FK parents the whole test shares; return (user_id, job_id).

    One user serves twice: as the creator of the job and as the uploader of the
    resume documents, which is what the two ``users`` foreign keys ask for.
    Candidates are *not* shared -- ``uq_job_applications_job_candidate`` allows
    one application per (job, candidate), so every application gets its own.
    """
    user_id = uuid4()
    session.add(
        User(
            id=user_id,
            username=f"hr-{user_id.hex[:12]}",
            password_hash="not-a-real-hash",
            role=UserRole.HR,
            is_active=True,
        )
    )
    job_id = uuid4()
    session.add(
        Job(
            id=job_id,
            title=f"Role {job_id.hex[:8]}",
            status=JobStatus.DRAFT,
            created_by=user_id,
        )
    )
    await session.flush()
    return user_id, job_id


async def _seed_document(
    session: AsyncSession,
    *,
    status: DocumentStatus,
    updated_at: datetime,
    uploaded_by: UUID,
) -> UUID:
    document = ResumeDocument(
        id=uuid4(),
        original_filename="resume.pdf",
        storage_key=f"objects/{uuid4().hex[:2]}/{uuid4().hex}",
        media_type="application/pdf",
        size_bytes=1024,
        content_sha256=uuid4().hex + uuid4().hex,
        status=status,
        attempt=0,
        uploaded_by=uploaded_by,
        updated_at=updated_at,
    )
    session.add(document)
    await session.flush()
    return document.id


async def _seed_pending_approval(
    session: AsyncSession,
    *,
    expires_at: datetime,
    index: int,
    job_id: UUID,
    approval_id: UUID | None = None,
) -> UUID:
    """Insert a run + application + PENDING approval, returning the approval id."""
    run_id = uuid4()
    application_id = uuid4()
    candidate_id = uuid4()
    session.add(Candidate(id=candidate_id, display_name=f"Candidate {candidate_id.hex[:8]}"))
    session.add(
        AgentRun(
            id=run_id,
            thread_id=f"thread-{run_id.hex}",
            run_type=RunType.APPLICATION,
            status=RunStatus.WAITING_APPROVAL,
            attempt=1,
            config_snapshot_json={},
        )
    )
    session.add(
        JobApplication(
            id=application_id,
            job_id=job_id,
            candidate_id=candidate_id,
            status=ApplicationStatus.CREATED,
            version=1,
        )
    )
    # See the module docstring: one level of the graph per flush.
    await session.flush()
    session.add(ApplicationRun(run_id=run_id, application_id=application_id))
    await session.flush()
    approval_id = approval_id or uuid4()
    session.add(
        Approval(
            id=approval_id,
            application_run_id=run_id,
            status=ApprovalStatus.PENDING,
            idempotency_key=f"{run_id}:1:UPDATE_APPLICATION_STATUS:{index}",
            version=1,
            expires_at=expires_at,
        )
    )
    await session.flush()
    return approval_id


def test_claim_expired_pending_is_ordered_and_skips_locked_rows() -> None:
    """Two concurrent sweepers split the backlog; neither blocks on the other.

    This is the FIN-006 requirement "多 Worker 竞争": with ``SKIP LOCKED`` the
    second worker skips rows the first holds instead of waiting, so the backlog
    drains faster than serialised batches would allow.
    """

    async def _run() -> None:
        engine, factory = _engine_and_factory()
        try:
            await _clear(engine)
            async with factory() as session:
                _, job_id = await _seed_context(session)
                for index in range(6):
                    await _seed_pending_approval(
                        session,
                        expires_at=NOW - timedelta(minutes=5),
                        index=index,
                        job_id=job_id,
                    )
                await session.commit()

            first_session = factory()
            second_session = factory()
            try:
                first_repo = SqlApprovalRepository(first_session)
                second_repo = SqlApprovalRepository(second_session)

                # The first claim holds row locks on the lowest-id batch.
                first = await first_repo.claim_expired_pending(NOW, limit=3)
                # The second claim must NOT block; it takes what is left.
                second = await second_repo.claim_expired_pending(NOW, limit=3)

                assert len(first) == 3
                assert len(second) == 3
                # Disjoint batches: no approval is handed to two sweepers.
                assert {a.id for a in first}.isdisjoint({a.id for a in second})
                # Stable ordering by id, both within and across the batches.
                ordered = [a.id for a in first] + [a.id for a in second]
                assert ordered == sorted(ordered)

                await first_session.commit()
                await second_session.commit()
            finally:
                await first_session.close()
                await second_session.close()
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    asyncio.run(_run())


def test_claim_expired_pending_ignores_rows_that_are_not_expired() -> None:
    """Only ``PENDING`` rows past the cutoff are claimed."""

    async def _run() -> None:
        engine, factory = _engine_and_factory()
        try:
            await _clear(engine)
            async with factory() as session:
                _, job_id = await _seed_context(session)
                expired_id = await _seed_pending_approval(
                    session,
                    expires_at=NOW - timedelta(minutes=1),
                    index=0,
                    job_id=job_id,
                )
                await _seed_pending_approval(
                    session,
                    expires_at=NOW + timedelta(hours=1),
                    index=1,
                    job_id=job_id,
                )
                await session.commit()

            async with factory() as session:
                repo = SqlApprovalRepository(session)
                claimed = await repo.claim_expired_pending(NOW, limit=10)
                assert [approval.id for approval in claimed] == [expired_id]
                await session.commit()
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    asyncio.run(_run())


def test_stranded_document_scan_filters_by_status_and_age() -> None:
    """The republish scan sees QUEUED-and-old, and nothing else (§14.4)."""

    async def _run() -> None:
        engine, factory = _engine_and_factory()
        try:
            await _clear(engine)
            async with factory() as session:
                user_id, _ = await _seed_context(session)
                stranded = await _seed_document(
                    session,
                    status=DocumentStatus.QUEUED,
                    updated_at=NOW - timedelta(hours=1),
                    uploaded_by=user_id,
                )
                await _seed_document(
                    session,
                    status=DocumentStatus.QUEUED,
                    updated_at=NOW,
                    uploaded_by=user_id,
                )
                await _seed_document(
                    session,
                    status=DocumentStatus.READY,
                    updated_at=NOW - timedelta(hours=1),
                    uploaded_by=user_id,
                )
                await _seed_document(
                    session,
                    status=DocumentStatus.PARSING,
                    updated_at=NOW - timedelta(hours=1),
                    uploaded_by=user_id,
                )
                await session.commit()

            async with factory() as session:
                repo = SqlMaintenanceDocumentRepository(session)
                found = await repo.list_stranded_parses(
                    queued_before=NOW - timedelta(minutes=2), limit=100
                )
                assert [document.id for document in found] == [stranded]
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    asyncio.run(_run())


def test_storage_key_reference_check_reflects_committed_rows() -> None:
    """The reference check is what protects a live object from deletion."""

    async def _run() -> None:
        engine, factory = _engine_and_factory()
        try:
            await _clear(engine)
            async with factory() as session:
                user_id, _ = await _seed_context(session)
                document_id = await _seed_document(
                    session,
                    status=DocumentStatus.READY,
                    updated_at=NOW,
                    uploaded_by=user_id,
                )
                document = await session.get(ResumeDocument, document_id)
                assert document is not None
                key = document.storage_key
                await session.commit()

            async with factory() as session:
                repo = SqlMaintenanceDocumentRepository(session)
                assert await repo.is_storage_key_referenced(key) is True
                assert (
                    await repo.is_storage_key_referenced(
                        f"objects/{uuid4().hex[:2]}/{uuid4().hex}"
                    )
                    is False
                )
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    asyncio.run(_run())


def test_recovery_probe_reads_checkpoint_and_executed_approval() -> None:
    """The probe distinguishes a resumable run from one that must wait for a human.

    Both halves matter: the checkpoint proves the graph can continue, and the
    ``EXECUTED`` approval proves a human authorised that continuation. The probe
    returning ``None`` for either keeps the sweep from proposing a resume.
    """

    async def _run() -> None:
        engine, factory = _engine_and_factory()
        try:
            await _clear(engine)
            run_id = uuid4()
            thread_id = f"thread-{run_id.hex}"
            decider = uuid4()
            async with factory() as session:
                _, job_id = await _seed_context(session)
                application_id = uuid4()
                candidate_id = uuid4()
                session.add(
                    Candidate(id=candidate_id, display_name=f"Candidate {candidate_id.hex[:8]}")
                )
                session.add(
                    AgentRun(
                        id=run_id,
                        thread_id=thread_id,
                        run_type=RunType.APPLICATION,
                        status=RunStatus.RUNNING,
                        attempt=2,
                        config_snapshot_json={},
                    )
                )
                session.add(
                    JobApplication(
                        id=application_id,
                        job_id=job_id,
                        candidate_id=candidate_id,
                        status=ApplicationStatus.CREATED,
                        version=1,
                    )
                )
                await session.flush()
                session.add(ApplicationRun(run_id=run_id, application_id=application_id))
                # Parents first, for the reason the module docstring gives.
                await session.flush()
                session.add(
                    Approval(
                        id=uuid4(),
                        application_run_id=run_id,
                        status=ApprovalStatus.EXECUTED,
                        idempotency_key=f"{run_id}:2:UPDATE_APPLICATION_STATUS:1",
                        version=1,
                        decided_by=decider,
                        executed_at=NOW,
                    )
                )
                await session.flush()
                session.add(
                    AgentCheckpoint(
                        thread_id=thread_id,
                        checkpoint_ns="",
                        checkpoint_id="ckpt-42",
                        checkpoint_json={},
                        metadata_json={},
                    )
                )
                await session.commit()

            async with factory() as session:
                probe = SqlApplicationRunRecoveryProbe(session)
                approval = await probe.executed_approval_for_run(run_id)
                assert approval is not None
                assert approval.decided_by == decider
                # A checkpoint exists for the run's thread, so the run is resumable.
                assert await probe.latest_checkpoint_version(thread_id) == "ckpt-42"
                # ...and a thread with no checkpoint is not.
                assert await probe.latest_checkpoint_version("missing-thread") is None
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    asyncio.run(_run())


def test_agent_run_scan_filters_status_type_and_age() -> None:
    """MatchRun republish candidates are CREATED + old + MATCH, nothing more."""

    async def _run() -> None:
        engine, factory = _engine_and_factory()
        try:
            await _clear(engine)
            async with factory() as session:
                stale_id = uuid4()
                session.add(
                    AgentRun(
                        id=stale_id,
                        thread_id=f"thread-{stale_id.hex}",
                        run_type=RunType.MATCH,
                        status=RunStatus.CREATED,
                        attempt=1,
                        config_snapshot_json={},
                        updated_at=NOW - timedelta(hours=2),
                    )
                )
                for run_type, status, updated in (
                    (RunType.MATCH, RunStatus.CREATED, NOW),
                    (RunType.MATCH, RunStatus.RUNNING, NOW - timedelta(hours=2)),
                    (RunType.MATCH, RunStatus.WAITING_APPROVAL, NOW - timedelta(hours=2)),
                    (RunType.APPLICATION, RunStatus.CREATED, NOW - timedelta(hours=2)),
                ):
                    run_id = uuid4()
                    session.add(
                        AgentRun(
                            id=run_id,
                            thread_id=f"thread-{run_id.hex}",
                            run_type=run_type,
                            status=status,
                            attempt=1,
                            config_snapshot_json={},
                            updated_at=updated,
                        )
                    )
                await session.commit()

            async with factory() as session:
                repo = SqlAgentRunRepository(session)
                found = await repo.list_stale_runs(
                    run_type=RunType.MATCH,
                    statuses=(RunStatus.CREATED,),
                    older_than=NOW - timedelta(minutes=30),
                    limit=100,
                )
                assert [run.id for run in found] == [stale_id]
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    asyncio.run(_run())
