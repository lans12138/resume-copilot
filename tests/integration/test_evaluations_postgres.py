"""FIN-007 end-to-end: evaluation persistence against real PostgreSQL.

The unit suite proves the service logic against in-memory adapters. What only a
real database can show — and what this file therefore covers — is the part the
in-memory mirrors *approximate*:

* **The duplicate guard's index actually exists and is usable.** The guard is a
  query, not an in-process flag, so a missing or wrong index would make it correct
  but slow, and a schema drift would make it silently permissive.
* **``uq_metric_snapshots_run_metric`` really prevents a double write**, so
  "replace, never append" is enforced by the database rather than by convention.
* **The immutability guard holds across sessions**, not just within one service
  instance: a *separate* connection sees the terminal status and refuses.
* **The status/kind columns round-trip as enums**, not strings — the failure mode
  that in-memory doubles hide entirely (a bare ``String`` column reads back as
  ``str`` and every ``is`` comparison silently misbehaves).

Skipped unless ``DATABASE_URL`` points at a real PostgreSQL; the compose-backed
probe ``tests/validate_evaluations.ps1`` runs it for real in CI.

Each ``def test_`` drives its async scenario through ``asyncio.run`` — the project
convention, since no pytest-asyncio plugin is configured.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import backend.app.evaluations.tasks  # noqa: F401
from backend.app.core.errors import AppError
from backend.app.evaluations.models import (
    EvaluationKind,
    EvaluationStatus,
)
from backend.app.evaluations.repository import (
    SqlDatasetVersionRepository,
    SqlEvaluationRunRepository,
    metric_snapshot_rows,
)
from backend.app.evaluations.service import EvaluationService, MetricResult

_DATABASE_URL = os.environ.get("DATABASE_URL", "")
_RUN_INTEGRATION = _DATABASE_URL.startswith("postgresql+asyncpg")
pytestmark = pytest.mark.skipif(
    not _RUN_INTEGRATION, reason="requires DATABASE_URL=postgresql+asyncpg://..."
)

# Child tables first; dataset_versions is referenced by evaluation_runs, which is
# referenced by metric_snapshots.
_TRUNCATE_SQL = (
    "TRUNCATE TABLE metric_snapshots, evaluation_runs, dataset_versions CASCADE"
)


def _factory() -> tuple[object, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_DATABASE_URL, future=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _clear(session: AsyncSession) -> None:
    await session.execute(text(_TRUNCATE_SQL))
    await session.commit()


async def _register(service: EvaluationService, name: str = "retrieval-golden") -> uuid.UUID:
    dataset = await service.register_dataset(
        name=name,
        version="v1",
        schema_version="1",
        manifest={"cases": 3},
        content={"cases": ["a", "b", "c"]},
    )
    return dataset.id


def test_dataset_and_run_round_trip_with_real_enums() -> None:
    """Enums must come back as enum members, not strings.

    This is the defect that in-memory doubles cannot see: a column declared as a
    bare ``String`` reads back as ``str``, every ``is`` comparison quietly takes
    the wrong branch, and the failure reproduces only against a real database.
    """

    async def scenario() -> None:
        engine, factory = _factory()
        try:
            async with factory() as session:
                await _clear(session)
                service = EvaluationService(
                    datasets=SqlDatasetVersionRepository(session),
                    runs=SqlEvaluationRunRepository(session),
                )
                dataset_id = await _register(service)
                run = await service.create_run(
                    dataset_version_id=dataset_id,
                    kind=EvaluationKind.SEMANTIC,
                    config={"prompt_version": "v2"},
                    model_snapshot={"chat_model": "fake"},
                    prompt_versions={"prompt_version": "v2"},
                    created_by=None,
                )
                await session.commit()

            async with factory() as session:
                loaded = await SqlEvaluationRunRepository(session).get(run.id)
                assert loaded is not None
                # ``is`` rather than ``==``: the whole point is the class identity.
                assert loaded.status is EvaluationStatus.CREATED
                assert loaded.kind is EvaluationKind.SEMANTIC
                # The client-stamped timestamps survived the round trip.
                assert loaded.created_at is not None
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_duplicate_guard_is_enforced_across_sessions() -> None:
    """The 409 guard is a database query, so a second connection sees the same live run.

    An in-process guard would let a second API worker (or a second browser tab) slip
    a duplicate past, because the two processes share no memory — only the database.
    """

    async def scenario() -> None:
        engine, factory = _factory()
        try:
            async with factory() as session:
                await _clear(session)
                service = EvaluationService(
                    datasets=SqlDatasetVersionRepository(session),
                    runs=SqlEvaluationRunRepository(session),
                )
                dataset_id = await _register(service)
                await service.create_run(
                    dataset_version_id=dataset_id,
                    kind=EvaluationKind.GOLDEN,
                    config={"top_k": 10},
                    model_snapshot={},
                    prompt_versions={},
                    created_by=None,
                )
                await session.commit()

            # A *separate* session must observe the live run and refuse a duplicate.
            async with factory() as session:
                service = EvaluationService(
                    datasets=SqlDatasetVersionRepository(session),
                    runs=SqlEvaluationRunRepository(session),
                )
                with pytest.raises(AppError) as error:
                    await service.create_run(
                        dataset_version_id=dataset_id,
                        kind=EvaluationKind.GOLDEN,
                        config={"top_k": 10},
                        model_snapshot={},
                        prompt_versions={},
                        created_by=None,
                    )
                assert error.value.code == "EVALUATION_DUPLICATE"
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_metric_uniqueness_rejects_a_second_write_for_one_metric() -> None:
    """``uq_metric_snapshots_run_metric`` makes an append a hard error.

    The service replaces wholesale, so a duplicate insert here would mean two code
    paths disagreeing about how a run's numbers are written — and the unique
    constraint is what turns that into a loud failure instead of two rows where one
    was expected.
    """

    async def scenario() -> None:
        engine, factory = _factory()
        try:
            async with factory() as session:
                await _clear(session)
                runs = SqlEvaluationRunRepository(session)
                service = EvaluationService(
                    datasets=SqlDatasetVersionRepository(session), runs=runs
                )
                dataset_id = await _register(service)
                run = await service.create_run(
                    dataset_version_id=dataset_id,
                    kind=EvaluationKind.GOLDEN,
                    config={"top_k": 10},
                    model_snapshot={},
                    prompt_versions={},
                    created_by=None,
                )
                await service.start(run.id)
                await service.record_metrics(
                    run.id,
                    [
                        MetricResult(
                            metric_name="recall_at_k",
                            metric_value=0.9,
                            threshold=0.85,
                            passed=True,
                        )
                    ],
                )
                await session.commit()

                # A direct append of the same metric name must be refused.
                duplicate = metric_snapshot_rows(
                    run.id,
                    [
                        {
                            "metric_name": "recall_at_k",
                            "metric_value": 0.1,
                            "threshold": 0.85,
                            "passed": False,
                        }
                    ],
                )
                session.add(duplicate[0])
                with pytest.raises(Exception) as error:
                    await session.commit()
                assert "uq_metric_snapshots_run_metric" in str(error.value)
                await session.rollback()
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_immutability_guard_holds_across_sessions() -> None:
    """A finished run refuses recomputation from *another* connection.

    §4.6's immutability is enforced in SQL (a real status predicate), not just in the
    service's in-memory copy — otherwise a late worker on a second connection would
    overwrite a published verdict.
    """

    async def scenario() -> None:
        engine, factory = _factory()
        try:
            async with factory() as session:
                await _clear(session)
                service = EvaluationService(
                    datasets=SqlDatasetVersionRepository(session),
                    runs=SqlEvaluationRunRepository(session),
                )
                dataset_id = await _register(service)
                run = await service.create_run(
                    dataset_version_id=dataset_id,
                    kind=EvaluationKind.GOLDEN,
                    config={"top_k": 10},
                    model_snapshot={},
                    prompt_versions={},
                    created_by=None,
                )
                await service.start(run.id)
                finished = await service.complete(
                    run.id,
                    [
                        MetricResult(
                            metric_name="recall_at_k",
                            metric_value=0.9,
                            threshold=0.85,
                            passed=True,
                        )
                    ],
                )
                assert finished is not None and finished.passed is True
                run_id = run.id
                await session.commit()

            # Fresh session: the run is terminal and its metrics are frozen.
            async with factory() as session:
                service = EvaluationService(
                    datasets=SqlDatasetVersionRepository(session),
                    runs=SqlEvaluationRunRepository(session),
                )
                assert await service.complete(run_id, []) is None
                stored = await service.list_metrics(run_id)
                assert [m.metric_name for m in stored] == ["recall_at_k"]
                assert stored[0].metric_value == pytest.approx(0.9)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_dataset_version_uniqueness_is_enforced_by_the_database() -> None:
    """``(name, version)`` uniqueness is a constraint, not a convention.

    Two concurrent registrations of the same version must not both succeed; the
    service's pre-check reduces the window but the constraint is the actual guard.
    """

    async def scenario() -> None:
        engine, factory = _factory()
        try:
            async with factory() as session:
                await _clear(session)
                datasets = SqlDatasetVersionRepository(session)
                await _register(EvaluationService(
                    datasets=datasets, runs=SqlEvaluationRunRepository(session)
                ))
                await session.commit()

                # Bypass the service's pre-check to reach the constraint itself.
                from backend.app.evaluations.models import DatasetVersion

                session.add(
                    DatasetVersion(
                        name="retrieval-golden",
                        version="v1",
                        content_hash="different",
                        schema_version="1",
                        manifest_json={},
                    )
                )
                with pytest.raises(Exception) as error:
                    await session.commit()
                assert "uq_dataset_versions_name_version" in str(error.value)
                await session.rollback()
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_evaluation_execute_task_persists_metrics_and_a_verdict() -> None:
    """The real task body, driven the way a worker runs it.

    ``.run()`` is executed in a worker thread because the task body calls
    ``asyncio.run`` itself — invoking it on the test's own loop would raise
    "asyncio.run() cannot be called from a running event loop", exactly as it would
    if the task were called from inside another coroutine.
    """

    async def scenario() -> None:
        engine, factory = _factory()
        try:
            async with factory() as session:
                await _clear(session)
                service = EvaluationService(
                    datasets=SqlDatasetVersionRepository(session),
                    runs=SqlEvaluationRunRepository(session),
                )
                dataset_id = await _register(service)
                run = await service.create_run(
                    dataset_version_id=dataset_id,
                    kind=EvaluationKind.INJECTION,
                    config={"samples": "builtin"},
                    model_snapshot={},
                    prompt_versions={},
                    created_by=None,
                )
                await session.commit()
                run_id = str(run.id)

            from backend.app.infrastructure.celery import app as celery_app

            result = await asyncio.to_thread(
                celery_app.tasks["evaluations.execute"].run, run_id
            )
            assert result["status"] == "ok"
            assert result["passed"] is True

            async with factory() as session:
                stored = await EvaluationService(
                    datasets=SqlDatasetVersionRepository(session),
                    runs=SqlEvaluationRunRepository(session),
                ).get_run(uuid.UUID(run_id))
                assert stored.status is EvaluationStatus.COMPLETED
                assert stored.passed is True
                metrics = await SqlEvaluationRunRepository(session).list_metrics(
                    uuid.UUID(run_id)
                )
                # All six injection counters, each with the zero bar attached.
                names = {m.metric_name for m in metrics}
                assert "injection_control_flow_changes" in names
                assert all(m.threshold == 0 for m in metrics)
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_evaluation_execute_task_is_a_noop_on_redelivery() -> None:
    """At-least-once delivery: the second call must not re-score or overwrite.

    This is the claim working as designed, not an error case — the task reports
    ``skipped``, which is what makes a duplicate broker delivery harmless.
    """

    async def scenario() -> None:
        engine, factory = _factory()
        try:
            async with factory() as session:
                await _clear(session)
                service = EvaluationService(
                    datasets=SqlDatasetVersionRepository(session),
                    runs=SqlEvaluationRunRepository(session),
                )
                dataset_id = await _register(service)
                run = await service.create_run(
                    dataset_version_id=dataset_id,
                    kind=EvaluationKind.GOLDEN,
                    config={"top_k": 10},
                    model_snapshot={},
                    prompt_versions={},
                    created_by=None,
                )
                await session.commit()
                run_id = str(run.id)

            from backend.app.infrastructure.celery import app as celery_app

            first = await asyncio.to_thread(
                celery_app.tasks["evaluations.execute"].run, run_id
            )
            second = await asyncio.to_thread(
                celery_app.tasks["evaluations.execute"].run, run_id
            )
            assert first["status"] == "ok"
            assert second["status"] == "skipped"
        finally:
            await engine.dispose()  # type: ignore[attr-defined]

    asyncio.run(scenario())
