"""Evaluation persistence ports and adapters (FIN-007).

Three repositories, one per §4.6 table. They are separate rather than one
aggregate repository because the tables have genuinely different lifecycles:
a ``DatasetVersion`` is written once and read forever, an ``EvaluationRun`` moves
through a small state machine, and ``MetricSnapshot`` rows are append-only while
the run is live and frozen the moment it is terminal.

Two rules hold across every adapter here, matching the ``maintenance`` precedent:

* **Terminal rows are never rewritten.** §4.6 requires that a finished run and its
  metrics be immutable, so no adapter exposes an update path for a terminal run's
  metrics. ``replace_metrics`` is only ever called on a ``RUNNING`` run, and the
  SQL adapter asserts that with a real ``status`` predicate rather than trusting
  the caller.
* **The in-memory adapters mirror SQL semantics, not a simplification.** Ordering,
  caps, and the duplicate-lookup behaviour are identical in both, because a test
  that passes against memory but would fail against PostgreSQL is worse than no
  test.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.evaluations.models import (
    TERMINAL_EVALUATION_STATUSES,
    DatasetVersion,
    EvaluationKind,
    EvaluationRun,
    EvaluationStatus,
    MetricSnapshot,
)

# Upper bound on page size for the list endpoint; keeps one request from pulling
# the whole history (§12.6 pagination).
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


class DatasetVersionRepository(Protocol):
    """Persistence contract for versioned evaluation datasets."""

    async def get(self, dataset_version_id: UUID) -> DatasetVersion | None: ...

    async def find(self, name: str, version: str) -> DatasetVersion | None:
        """Look up one version by its natural key (``name`` + ``version``)."""
        ...

    async def add(self, dataset: DatasetVersion) -> None: ...

    async def list_all(self) -> list[DatasetVersion]: ...


class EvaluationRunRepository(Protocol):
    """Persistence contract for evaluation runs and their metric rows."""

    async def get(self, evaluation_run_id: UUID) -> EvaluationRun | None: ...

    async def add(self, run: EvaluationRun) -> None: ...

    async def update(self, run: EvaluationRun) -> None: ...

    async def find_recent(
        self, *, dataset_version_id: UUID, config_hash: str, kinds: tuple[EvaluationStatus, ...]
    ) -> EvaluationRun | None:
        """Most recent run for one (dataset, config) in any of ``kinds``.

        Backs the duplicate-execution guard: re-submitting an evaluation that is
        still queued or running must fail with ``EVALUATION_DUPLICATE`` rather
        than enqueue a second, identical verdict.
        """
        ...

    async def list_page(
        self,
        *,
        status: EvaluationStatus | None,
        kind: EvaluationKind | None,
        limit: int,
        offset: int,
    ) -> list[EvaluationRun]: ...

    async def count(
        self, *, status: EvaluationStatus | None, kind: EvaluationKind | None
    ) -> int: ...

    async def replace_metrics(self, evaluation_run_id: UUID, metrics: list[MetricSnapshot]) -> None:
        """Delete and re-write a run's metric rows (RUNNING runs only).

        Replace rather than append: the unique constraint on
        ``(evaluation_run_id, metric_name)`` means a second pass over the same
        suite would collide, and the correct semantic for "compute this run's
        numbers" is that the new numbers supersede the old ones wholesale.
        """
        ...

    async def list_metrics(self, evaluation_run_id: UUID) -> list[MetricSnapshot]: ...


class SqlDatasetVersionRepository:
    """PostgreSQL adapter over ``dataset_versions``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, dataset_version_id: UUID) -> DatasetVersion | None:
        return await self._session.get(DatasetVersion, dataset_version_id)

    async def find(self, name: str, version: str) -> DatasetVersion | None:
        result = await self._session.execute(
            select(DatasetVersion).where(
                DatasetVersion.name == name, DatasetVersion.version == version
            )
        )
        return result.scalars().first()

    async def add(self, dataset: DatasetVersion) -> None:
        self._session.add(dataset)

    async def list_all(self) -> list[DatasetVersion]:
        result = await self._session.execute(
            select(DatasetVersion).order_by(DatasetVersion.name, DatasetVersion.version)
        )
        return list(result.scalars().all())


class SqlEvaluationRunRepository:
    """PostgreSQL adapter over ``evaluation_runs`` and ``metric_snapshots``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, evaluation_run_id: UUID) -> EvaluationRun | None:
        return await self._session.get(EvaluationRun, evaluation_run_id)

    async def add(self, run: EvaluationRun) -> None:
        self._session.add(run)

    async def update(self, run: EvaluationRun) -> None:
        self._session.add(run)

    async def find_recent(
        self, *, dataset_version_id: UUID, config_hash: str, kinds: tuple[EvaluationStatus, ...]
    ) -> EvaluationRun | None:
        if not kinds:
            return None
        result = await self._session.execute(
            select(EvaluationRun)
            .where(
                EvaluationRun.dataset_version_id == dataset_version_id,
                EvaluationRun.config_hash == config_hash,
                EvaluationRun.status.in_(kinds),
            )
            .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
        )
        return result.scalars().first()

    async def list_page(
        self,
        *,
        status: EvaluationStatus | None,
        kind: EvaluationKind | None,
        limit: int,
        offset: int,
    ) -> list[EvaluationRun]:
        statement = select(EvaluationRun)
        if status is not None:
            statement = statement.where(EvaluationRun.status == status)
        if kind is not None:
            statement = statement.where(EvaluationRun.kind == kind)
        result = await self._session.execute(
            statement.order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count(self, *, status: EvaluationStatus | None, kind: EvaluationKind | None) -> int:
        statement = select(EvaluationRun.id)
        if status is not None:
            statement = statement.where(EvaluationRun.status == status)
        if kind is not None:
            statement = statement.where(EvaluationRun.kind == kind)
        result = await self._session.execute(statement)
        return len(list(result.scalars().all()))

    async def replace_metrics(self, evaluation_run_id: UUID, metrics: list[MetricSnapshot]) -> None:
        # The status predicate is the immutability guard, enforced in SQL rather
        # than trusted from the caller: a run that already finished cannot have
        # its numbers rewritten by a late or duplicated worker.
        run = await self._session.get(EvaluationRun, evaluation_run_id)
        if run is None or run.status in TERMINAL_EVALUATION_STATUSES:
            return
        await self._session.execute(
            delete(MetricSnapshot).where(MetricSnapshot.evaluation_run_id == evaluation_run_id)
        )
        for metric in metrics:
            self._session.add(metric)
        await self._session.flush()

    async def list_metrics(self, evaluation_run_id: UUID) -> list[MetricSnapshot]:
        result = await self._session.execute(
            select(MetricSnapshot)
            .where(MetricSnapshot.evaluation_run_id == evaluation_run_id)
            .order_by(MetricSnapshot.metric_name)
        )
        return list(result.scalars().all())


class InMemoryDatasetVersionRepository:
    """Process-local mirror of the SQL adapter for hermetic tests."""

    def __init__(self, datasets: dict[UUID, DatasetVersion] | None = None) -> None:
        self._datasets = datasets if datasets is not None else {}

    async def get(self, dataset_version_id: UUID) -> DatasetVersion | None:
        return self._datasets.get(dataset_version_id)

    async def find(self, name: str, version: str) -> DatasetVersion | None:
        for dataset in self._datasets.values():
            if dataset.name == name and dataset.version == version:
                return dataset
        return None

    async def add(self, dataset: DatasetVersion) -> None:
        self._datasets[dataset.id] = dataset

    async def list_all(self) -> list[DatasetVersion]:
        return sorted(self._datasets.values(), key=lambda d: (d.name, d.version))


class InMemoryEvaluationRunRepository:
    """Process-local mirror of the SQL adapter for hermetic tests.

    Ordering matches SQL: ``created_at`` descending with the id as the tiebreak,
    so a test that asserts "newest first" means the same thing in both adapters.
    """

    def __init__(
        self,
        runs: dict[UUID, EvaluationRun] | None = None,
        metrics: dict[UUID, list[MetricSnapshot]] | None = None,
    ) -> None:
        self._runs = runs if runs is not None else {}
        self._metrics = metrics if metrics is not None else {}

    async def get(self, evaluation_run_id: UUID) -> EvaluationRun | None:
        return self._runs.get(evaluation_run_id)

    async def add(self, run: EvaluationRun) -> None:
        self._runs[run.id] = run

    async def update(self, run: EvaluationRun) -> None:
        self._runs[run.id] = run

    async def find_recent(
        self, *, dataset_version_id: UUID, config_hash: str, kinds: tuple[EvaluationStatus, ...]
    ) -> EvaluationRun | None:
        if not kinds:
            return None
        matches = [
            run
            for run in self._runs.values()
            if run.dataset_version_id == dataset_version_id
            and run.config_hash == config_hash
            and run.status in kinds
        ]
        matches.sort(key=lambda r: (r.created_at, str(r.id)), reverse=True)
        return matches[0] if matches else None

    async def list_page(
        self,
        *,
        status: EvaluationStatus | None,
        kind: EvaluationKind | None,
        limit: int,
        offset: int,
    ) -> list[EvaluationRun]:
        rows = [
            run
            for run in self._runs.values()
            if (status is None or run.status == status) and (kind is None or run.kind == kind)
        ]
        rows.sort(key=lambda r: (r.created_at, str(r.id)), reverse=True)
        return rows[offset : offset + limit]

    async def count(self, *, status: EvaluationStatus | None, kind: EvaluationKind | None) -> int:
        return sum(
            1
            for run in self._runs.values()
            if (status is None or run.status == status) and (kind is None or run.kind == kind)
        )

    async def replace_metrics(self, evaluation_run_id: UUID, metrics: list[MetricSnapshot]) -> None:
        run = self._runs.get(evaluation_run_id)
        if run is None or run.status in TERMINAL_EVALUATION_STATUSES:
            return
        self._metrics[evaluation_run_id] = list(metrics)

    async def list_metrics(self, evaluation_run_id: UUID) -> list[MetricSnapshot]:
        rows = list(self._metrics.get(evaluation_run_id, []))
        rows.sort(key=lambda m: m.metric_name)
        return rows


def metric_snapshot_rows(
    evaluation_run_id: UUID, metrics: list[dict[str, Any]], *, created_at: datetime | None = None
) -> list[MetricSnapshot]:
    """Build metric rows from the scorer's plain dicts, in one place.

    Kept as a module function rather than a method so both the task and the
    service can build rows without duplicating the field mapping — and so the
    mapping is unit-testable without a database.

    ``created_at`` is accepted rather than left to the column default for the same
    reason the run and dataset stamp theirs: the in-memory adapter must produce
    rows indistinguishable from the SQL adapter's.
    """
    rows: list[MetricSnapshot] = []
    for entry in metrics:
        snapshot = MetricSnapshot(
            evaluation_run_id=evaluation_run_id,
            metric_name=str(entry["metric_name"]),
            metric_value=float(entry["metric_value"]),
            threshold=None if entry.get("threshold") is None else float(entry["threshold"]),
            passed=bool(entry["passed"]),
            dimensions_json=dict(entry.get("dimensions_json") or {}),
        )
        if created_at is not None:
            snapshot.created_at = created_at
        rows.append(snapshot)
    return rows


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "DatasetVersionRepository",
    "EvaluationRunRepository",
    "InMemoryDatasetVersionRepository",
    "InMemoryEvaluationRunRepository",
    "SqlDatasetVersionRepository",
    "SqlEvaluationRunRepository",
    "metric_snapshot_rows",
]
