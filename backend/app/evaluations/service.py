"""Evaluation orchestration: dataset registration, gating, and verdicts (FIN-007).

The *scoring* already lives in three hermetic modules — ``retrieval.golden``
(Recall@K), ``evaluations.semantic`` (support-label F1), and
``evaluations.injection`` (prompt-injection attack success). This module does the
part that was missing: turning those pure verdicts into durable,
attributable rows, and refusing work that would produce a second identical
verdict.

Design points worth stating explicitly, because each is a decision rather than an
implementation detail:

**The duplicate guard is on (dataset, config), not on the caller.**
  §12.6 specifies ``409 EVALUATION_DUPLICATE``. Keying it on the pair
  ``(dataset_version_id, config_hash)`` means a caller who re-submits an
  in-flight evaluation — double-click, retried request, a CI job that ran twice —
  gets a conflict instead of a second run that would consume model budget and
  produce a verdict indistinguishable from the first. It also means the guard
  cannot be defeated by a different ``actor``, which a per-user key would allow.

**A finished run is never recomputed.**
  §4.6 forbids editing ``EvaluationRun``/``MetricSnapshot`` in place. So
  ``complete`` and ``fail`` refuse to move a run that is already terminal; a
  re-run is a new row. This is what makes a cited verdict stable: the numbers
  behind "CI was green at this commit" cannot be silently overwritten later.

**Failures are recorded as ``FAILED`` with a safe code, not raised away.**
  A gate that *fails its threshold* is a ``COMPLETED`` run carrying
  ``passed=False`` — that is a normal, expected outcome. A run only becomes
  ``FAILED`` when the evaluation could not be carried out at all (scorer raised,
  dataset unreadable). Conflating the two would make "the model regressed"
  indistinguishable from "the harness crashed", which is exactly the distinction
  the gate exists to surface.

**Metric rows are per-threshold, not one run-level boolean.**
  ``MetricSnapshot`` carries ``threshold`` and ``passed`` per metric so a reader
  can answer "which bar was missed, and by how much" without re-running the
  scorer — and a historical row keeps the bar it was actually measured against.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from backend.app.core.errors import AppError
from backend.app.evaluations.models import (
    DatasetVersion,
    EvaluationKind,
    EvaluationRun,
    EvaluationStatus,
    is_terminal,
)
from backend.app.evaluations.repository import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DatasetVersionRepository,
    EvaluationRunRepository,
    metric_snapshot_rows,
)

logger = logging.getLogger(__name__)

# Live statuses that a duplicate check must consider "already in flight".
ACTIVE_EVALUATION_STATUSES: tuple[EvaluationStatus, ...] = (
    EvaluationStatus.CREATED,
    EvaluationStatus.RUNNING,
)


def config_digest(config: Mapping[str, Any]) -> str:
    """Stable digest of an evaluation config.

    Canonicalised with sorted keys so two callers who pass the same settings in a
    different insertion order compute the *same* hash — otherwise the duplicate
    guard would be trivially bypassable by reordering a dict, which is not a
    meaningful distinction between two requests.
    """
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_digest(content: Any) -> str:
    """Stable digest of a dataset's canonical content."""
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MetricResult:
    """One scored metric, ready to persist."""

    metric_name: str
    metric_value: float
    passed: bool
    threshold: float | None = None
    dimensions_json: dict[str, Any] = field(default_factory=dict)


class EvaluationExecutor(Protocol):
    """Runs one evaluation suite and returns its metric list.

    A port rather than a direct call so the task holds no scoring logic: the
    suites are pure functions over hermetic datasets, and the same port is
    satisfied by the built-in runner and by a test double that emits a deliberately
    failing metric.
    """

    def run(self, kind: EvaluationKind) -> list[MetricResult]: ...


@dataclass(frozen=True, slots=True)
class EvaluationPage:
    """One page of evaluation runs plus the total, for §12.6 pagination."""

    items: list[EvaluationRun]
    total: int
    limit: int
    offset: int


class EvaluationService:
    """Registers datasets, gates duplicate submissions, and records verdicts."""

    def __init__(
        self,
        *,
        datasets: DatasetVersionRepository,
        runs: EvaluationRunRepository,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._datasets = datasets
        self._runs = runs
        self._now = now or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Dataset registration
    # ------------------------------------------------------------------

    async def register_dataset(
        self,
        *,
        name: str,
        version: str,
        schema_version: str,
        manifest: Mapping[str, Any],
        content: Any,
    ) -> DatasetVersion:
        """Upsert one dataset version, keyed on ``(name, version)``.

        Idempotent by natural key: re-registering an identical version returns the
        existing row rather than violating the unique constraint. A *different*
        content hash under the same version is refused, because silently
        overwriting it would invalidate every verdict already attributed to that
        version — the whole point of versioning the data is that a cited version
        means one fixed thing.
        """
        digest = content_digest(content)
        existing = await self._datasets.find(name, version)
        if existing is not None:
            if existing.content_hash != digest:
                raise AppError(
                    code="DATASET_VERSION_IMMUTABLE",
                    http_status=409,
                    safe_message="该数据集版本已存在且内容不同，请发布新版本",
                    details={"name": name, "version": version},
                )
            return existing
        dataset = DatasetVersion(
            # Client-generated for the same reason as the run id: callers use the id
            # immediately, before any flush.
            id=uuid4(),
            name=name,
            version=version,
            content_hash=digest,
            schema_version=schema_version,
            manifest_json=dict(manifest),
            # Client-stamped for the same reason as ``created_at`` on a run: both
            # adapters must order and compare identically.
            created_at=self._now(),
        )
        await self._datasets.add(dataset)
        return dataset

    # ------------------------------------------------------------------
    # Submission (§12.6 POST /evaluations)
    # ------------------------------------------------------------------

    async def create_run(
        self,
        *,
        dataset_version_id: UUID,
        kind: EvaluationKind,
        config: Mapping[str, Any],
        model_snapshot: Mapping[str, Any],
        prompt_versions: Mapping[str, Any],
        created_by: UUID | None,
    ) -> EvaluationRun:
        """Create a ``CREATED`` run, or raise ``EVALUATION_DUPLICATE``.

        The returned run is *not* started here: the caller commits it and then
        publishes the task, so the API can answer ``202`` without holding a
        transaction open across a model call (§14.2 — commit the fact, then
        publish).
        """
        dataset = await self._datasets.get(dataset_version_id)
        if dataset is None:
            raise AppError(
                code="DATASET_VERSION_NOT_FOUND",
                http_status=404,
                safe_message="指定的数据集版本不存在",
                details={"dataset_version_id": str(dataset_version_id)},
            )

        digest = config_digest(config)
        duplicate = await self._runs.find_recent(
            dataset_version_id=dataset_version_id,
            config_hash=digest,
            kinds=ACTIVE_EVALUATION_STATUSES,
        )
        if duplicate is not None:
            raise AppError(
                code="EVALUATION_DUPLICATE",
                http_status=409,
                safe_message="同一数据集与配置的评测已在执行中",
                details={"evaluation_run_id": str(duplicate.id)},
            )

        run = EvaluationRun(
            # The id is generated client-side rather than left to the column default.
            # The API returns this id in its 202 *before* the session flushes, and the
            # delivery is published against it, so it must exist the moment the object
            # does — a database-assigned default would mean publishing ``None``.
            id=uuid4(),
            dataset_version_id=dataset_version_id,
            kind=kind,
            status=EvaluationStatus.CREATED,
            config_hash=digest,
            model_snapshot_json=dict(model_snapshot),
            prompt_versions_json=dict(prompt_versions),
            created_by=created_by,
            # Stamped client-side rather than left to the column's ``server_default``.
            # The in-memory adapter mirrors SQL semantics, and a row whose
            # ``created_at`` is only populated by the database would order
            # differently in the two adapters — so a test that passes against memory
            # could fail against PostgreSQL (or the reverse).
            created_at=self._now(),
        )
        await self._runs.add(run)
        return run

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def start(self, evaluation_run_id: UUID) -> EvaluationRun | None:
        """Move a ``CREATED`` run to ``RUNNING``; ``None`` if it is not claimable.

        Returning ``None`` rather than raising is deliberate: this is the
        at-least-once claim path, and a second delivery of a task whose run is
        already running or finished is an expected no-op, not an error.
        """
        run = await self._runs.get(evaluation_run_id)
        if run is None or run.status is not EvaluationStatus.CREATED:
            return None
        run.status = EvaluationStatus.RUNNING
        run.started_at = self._now()
        await self._runs.update(run)
        return run

    async def record_metrics(self, evaluation_run_id: UUID, metrics: list[MetricResult]) -> None:
        """Persist a run's metric rows (no-op unless the run is ``RUNNING``)."""
        rows = metric_snapshot_rows(
            evaluation_run_id,
            [
                {
                    "metric_name": metric.metric_name,
                    "metric_value": metric.metric_value,
                    "threshold": metric.threshold,
                    "passed": metric.passed,
                    "dimensions_json": metric.dimensions_json,
                }
                for metric in metrics
            ],
            created_at=self._now(),
        )
        await self._runs.replace_metrics(evaluation_run_id, rows)

    async def complete(
        self, evaluation_run_id: UUID, metrics: list[MetricResult]
    ) -> EvaluationRun | None:
        """Record metrics and finish the run with an overall verdict.

        The overall ``passed`` is the conjunction of every metric that declares a
        threshold. Metrics without a threshold are informational (e.g. case count)
        and cannot fail the run — otherwise adding a diagnostic metric would
        change the gate outcome, which would be a surprising coupling.

        A run with *no* thresholded metric is recorded as **not passed**: ``all()``
        over nothing is true, and a run that produced no scores has demonstrated
        nothing. Reporting that as a pass would turn "the scorer returned nothing"
        into a green gate.
        """
        run = await self._runs.get(evaluation_run_id)
        if run is None or is_terminal(run.status):
            return None
        if run.status is not EvaluationStatus.RUNNING:
            run.status = EvaluationStatus.RUNNING
            run.started_at = run.started_at or self._now()
        await self.record_metrics(evaluation_run_id, metrics)
        thresholded = [metric for metric in metrics if metric.threshold is not None]
        run.passed = bool(thresholded) and all(metric.passed for metric in thresholded)
        run.status = EvaluationStatus.COMPLETED
        run.finished_at = self._now()
        await self._runs.update(run)
        return run

    async def fail(
        self, evaluation_run_id: UUID, *, error_code: str, safe_message: str
    ) -> EvaluationRun | None:
        """Mark a run ``FAILED`` because the evaluation could not be carried out.

        Distinct from ``complete(passed=False)``: that means the gate ran and the
        numbers missed the bar, which is a legitimate outcome. This means the
        harness itself broke, and the two must not be confused in the history.
        """
        run = await self._runs.get(evaluation_run_id)
        if run is None or is_terminal(run.status):
            return None
        run.status = EvaluationStatus.FAILED
        run.error_code = error_code
        run.error_message_safe = safe_message
        run.finished_at = self._now()
        await self._runs.update(run)
        return run

    # ------------------------------------------------------------------
    # Reads (§12.6 GET /evaluations, GET /evaluations/{id})
    # ------------------------------------------------------------------

    async def get_run(self, evaluation_run_id: UUID) -> EvaluationRun:
        run = await self._runs.get(evaluation_run_id)
        if run is None:
            raise AppError(
                code="EVALUATION_NOT_FOUND",
                http_status=404,
                safe_message="评测不存在",
                details={"evaluation_run_id": str(evaluation_run_id)},
            )
        return run

    async def get_dataset(self, dataset_version_id: UUID) -> DatasetVersion | None:
        """The dataset a run was scored against, for the detail view (§12.6)."""
        return await self._datasets.get(dataset_version_id)

    async def list_metrics(self, evaluation_run_id: UUID) -> list[MetricResult]:
        rows = await self._runs.list_metrics(evaluation_run_id)
        return [
            MetricResult(
                metric_name=row.metric_name,
                metric_value=float(row.metric_value),
                passed=row.passed,
                threshold=None if row.threshold is None else float(row.threshold),
                dimensions_json=dict(row.dimensions_json),
            )
            for row in rows
        ]

    async def list_page(
        self,
        *,
        status: EvaluationStatus | None = None,
        kind: EvaluationKind | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> EvaluationPage:
        """Paginated history, newest first, with the page size clamped."""
        effective_page = max(1, page)
        effective_size = min(MAX_PAGE_SIZE, max(1, page_size))
        offset = (effective_page - 1) * effective_size
        items = await self._runs.list_page(
            status=status, kind=kind, limit=effective_size, offset=offset
        )
        total = await self._runs.count(status=status, kind=kind)
        return EvaluationPage(items=items, total=total, limit=effective_size, offset=offset)


__all__ = [
    "ACTIVE_EVALUATION_STATUSES",
    "EvaluationExecutor",
    "EvaluationPage",
    "EvaluationService",
    "MetricResult",
    "config_digest",
    "content_digest",
]
