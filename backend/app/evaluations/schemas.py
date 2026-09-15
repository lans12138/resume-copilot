"""Evaluation API schemas (FIN-007, §12.6).

The response shapes are deliberately *structured* rather than prose: a client must
be able to render "which gate failed and by how much" from fields, never by
parsing a message. ``passed`` and ``threshold`` are carried per metric for exactly
that reason — the run-level ``passed`` summarises the conjunction, but the detail
view answers the question a reader actually has.

Nothing here exposes model credentials or raw dataset payloads. The run's
``model_snapshot_json`` is included because knowing *which model version* produced
a verdict is the point of recording it; the dataset's ``manifest_json`` is a
bounded description, not the case payload.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.app.evaluations.models import (
    DatasetVersion,
    EvaluationKind,
    EvaluationRun,
    EvaluationStatus,
)


class EvaluationCreateRequest(BaseModel):
    """Body of ``POST /evaluations`` (§12.6).

    ``config`` is free-form because the comparison axes differ per suite (retrieval
    weights, model temperature, prompt versions). It is hashed rather than
    interpreted, so the duplicate guard does not need to know its shape.
    """

    dataset_version_id: UUID
    kind: EvaluationKind
    config: dict[str, Any] = Field(default_factory=dict)


class DatasetVersionSummary(BaseModel):
    """Bounded description of the dataset a run was scored against."""

    id: UUID
    name: str
    version: str
    content_hash: str
    schema_version: str
    manifest: dict[str, Any]

    @classmethod
    def from_model(cls, dataset: DatasetVersion) -> DatasetVersionSummary:
        return cls(
            id=dataset.id,
            name=dataset.name,
            version=dataset.version,
            content_hash=dataset.content_hash,
            schema_version=dataset.schema_version,
            manifest=dict(dataset.manifest_json),
        )


class MetricSnapshotView(BaseModel):
    """One stored metric, with the bar it was measured against (§4.6)."""

    metric_name: str
    metric_value: float
    threshold: float | None
    passed: bool
    dimensions: dict[str, Any]

    @classmethod
    def from_metric(cls, metric: Any) -> MetricSnapshotView:
        return cls(
            metric_name=metric.metric_name,
            metric_value=float(metric.metric_value),
            threshold=None if metric.threshold is None else float(metric.threshold),
            passed=metric.passed,
            dimensions=dict(metric.dimensions_json),
        )


class EvaluationRunSummary(BaseModel):
    """List-row view; carries no metric detail to keep the page cheap (§12.6)."""

    id: UUID
    dataset_version_id: UUID
    kind: EvaluationKind
    status: EvaluationStatus
    config_hash: str
    passed: bool | None
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    failing_metric_count: int = 0

    @classmethod
    def from_run(cls, run: EvaluationRun, *, failing_metric_count: int = 0) -> EvaluationRunSummary:
        return cls(
            id=run.id,
            dataset_version_id=run.dataset_version_id,
            kind=run.kind,
            status=run.status,
            config_hash=run.config_hash,
            passed=run.passed,
            error_code=run.error_code,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            failing_metric_count=failing_metric_count,
        )


class EvaluationAccepted(BaseModel):
    """``202`` body returned by ``POST /evaluations`` (§12.6)."""

    evaluation_run_id: UUID
    status: EvaluationStatus
    kind: EvaluationKind
    dataset_version_id: UUID
    config_hash: str


class EvaluationList(BaseModel):
    """Paginated evaluation history (§12.6)."""

    items: list[EvaluationRunSummary]
    total: int
    limit: int
    offset: int


class EvaluationDetail(BaseModel):
    """Full verdict: the run, its dataset, its model/prompt pins, and metrics."""

    run: EvaluationRunSummary
    dataset: DatasetVersionSummary
    model_snapshot: dict[str, Any]
    prompt_versions: dict[str, Any]
    error_message_safe: str | None
    metrics: list[MetricSnapshotView]

    model_config = ConfigDict(populate_by_name=True)

    @property
    def failing_metrics(self) -> list[MetricSnapshotView]:
        """Thresholded metrics that missed their bar (computed, never stored)."""
        return [
            metric for metric in self.metrics if metric.threshold is not None and not metric.passed
        ]


__all__ = [
    "DatasetVersionSummary",
    "EvaluationAccepted",
    "EvaluationCreateRequest",
    "EvaluationDetail",
    "EvaluationList",
    "EvaluationRunSummary",
    "MetricSnapshotView",
]
