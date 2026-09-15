"""Evaluation persistence: datasets, runs, and metric snapshots (FIN-007).

Detailed design §4.6 names three tables. Together they make an *offline* gate
verdict durable: what was evaluated (``DatasetVersion``), how it was run
(``EvaluationRun``), and what the numbers were (``MetricSnapshot`` rows).

Two invariants drive every decision in this module:

* **A finished run is immutable.** §4.6 is explicit that ``EvaluationRun`` and
  ``MetricSnapshot`` are never edited in place; a re-run is a *new* run. That is
  what makes a published gate verdict auditable — the row you cite today is
  byte-identical to the row that produced yesterday's CI signal. The service
  layer enforces this by refusing to mutate a run outside ``RUNNING``; this
  module supplies the status vocabulary that rule is written against.
* **The verdict must be reproducible from the row alone.** ``config_hash`` and
  ``model_snapshot_json`` exist so a verdict can be attributed: two runs with the
  same dataset, config, and model are expected to agree, and if they do not, the
  difference is a real regression rather than a mystery. ``prompt_versions_json``
  extends that to the prompt templates, which change independently of model
  weights.

``MetricSnapshot`` deliberately stores ``threshold`` and ``passed`` *per metric*
rather than a single run-level boolean. The gate has several independent
thresholds (Recall@K, macro-F1, attack-success counts) and a run-level boolean
would force a caller to re-derive which one failed; keeping them per row means
the detail endpoint can answer "what exactly failed, and by how much" without
re-reading the evaluation code that produced it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database import Base


class EvaluationStatus(StrEnum):
    """Lifecycle of one ``EvaluationRun``.

    ``CREATED`` -> ``RUNNING`` -> one of ``COMPLETED`` / ``FAILED``. The three
    terminal values are mutually exclusive and, per §4.6, final: a re-run creates
    a new row rather than reviving a finished one.
    """

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# A finished evaluation never moves again. Declared next to the enum so adding a
# status forces a reader to decide whether it is terminal, mirroring
# ``agent.models.TERMINAL_RUN_STATUSES``.
TERMINAL_EVALUATION_STATUSES: frozenset[EvaluationStatus] = frozenset(
    {EvaluationStatus.COMPLETED, EvaluationStatus.FAILED}
)


def is_terminal(status: EvaluationStatus) -> bool:
    """True when the evaluation has reached a state it can never leave."""
    return status in TERMINAL_EVALUATION_STATUSES


class EvaluationKind(StrEnum):
    """Which evaluation suite a run belongs to (§9.4, §9.5, §18.3).

    The three suites have different requirements and different gate thresholds,
    so the kind is persisted rather than inferred from the metric names: a
    ``semantic`` run that happens to emit a ``recall_at_k`` row must not be read
    as a retrieval run.
    """

    GOLDEN = "GOLDEN"
    SEMANTIC = "SEMANTIC"
    INJECTION = "INJECTION"


class DatasetVersion(Base):
    """One immutable version of a versioned evaluation dataset (§4.6).

    ``content_hash`` is the integrity anchor: the manifest alone is not enough to
    prove two runs used the same data, because a manifest can be re-serialised in
    a different order. Hashing the canonical content lets a reader assert equality
    without trusting the JSON layout.
    """

    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_dataset_versions_name_version"),
        CheckConstraint("length(content_hash) > 0", name="ck_dataset_versions_hash_nonempty"),
        Index("ix_dataset_versions_name_version", "name", "version"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Dataset family, e.g. "retrieval-golden" / "support-labels" / "injection-pairs".
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    # Digest over the canonical content; the equality anchor for "same data".
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Schema generation, so a loader can refuse a payload it cannot parse.
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    # Bounded description of the dataset; never carries the full case payload.
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EvaluationRun(Base):
    """One execution of a dataset under a fixed config, model, and prompts.

    Immutable once terminal (§4.6): the service only writes metrics while the run
    is ``RUNNING``. ``config_hash`` is what the duplicate-execution guard keys on
    — re-submitting the same dataset+config is rejected with
    ``EVALUATION_DUPLICATE`` (409) rather than silently producing a second,
    identical verdict.
    """

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dataset_version_id"],
            ["dataset_versions.id"],
            name="fk_evaluation_runs_dataset",
        ),
        CheckConstraint(
            "status IN ('CREATED','RUNNING','COMPLETED','FAILED')",
            name="ck_evaluation_runs_status",
        ),
        CheckConstraint(
            "kind IN ('GOLDEN','SEMANTIC','INJECTION')",
            name="ck_evaluation_runs_kind",
        ),
        Index("ix_evaluation_runs_status", "status"),
        Index("ix_evaluation_runs_dataset", "dataset_version_id"),
        # Supports the duplicate guard: find a live run for the same (dataset,
        # config) without scanning every historical evaluation.
        Index(
            "ix_evaluation_runs_dedupe",
            "dataset_version_id",
            "config_hash",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    dataset_version_id: Mapped[UUID] = mapped_column(nullable=False)
    kind: Mapped[EvaluationKind] = mapped_column(
        Enum(EvaluationKind, native_enum=False, create_constraint=False, length=16),
        nullable=False,
    )
    status: Mapped[EvaluationStatus] = mapped_column(
        Enum(EvaluationStatus, native_enum=False, create_constraint=False, length=16),
        default=EvaluationStatus.CREATED,
        nullable=False,
    )
    # Digest over the effective config; the other half of the duplicate guard.
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Pins which model weights produced the numbers (§18.2 reproducibility).
    model_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Pins prompt template versions, which move independently of model weights.
    prompt_versions_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Overall verdict, NULL until the run finishes. Mirrors the per-metric
    # ``passed`` flags but is kept denormalised so list views need no join.
    passed: Mapped[bool | None] = mapped_column(Boolean)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message_safe: Mapped[str | None] = mapped_column(String(500))
    # Initiating actor; NULL for scheduled/CI runs.
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MetricSnapshot(Base):
    """One measured metric of one evaluation run (§4.6).

    ``threshold`` and ``passed`` live on the row, not on the run, because the gate
    is a conjunction of independent thresholds. Storing them per metric is what
    lets ``GET /evaluations/{id}`` report *which* gate failed without re-running
    the scoring code — and it keeps the stored verdict honest if a threshold is
    later tuned, since the historical row still records the bar it was measured
    against.
    """

    __tablename__ = "metric_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["evaluation_run_id"],
            ["evaluation_runs.id"],
            name="fk_metric_snapshots_run",
        ),
        # One value per metric per run: a second write for the same name is a
        # programming error (the run is being recomputed), not a valid update.
        UniqueConstraint("evaluation_run_id", "metric_name", name="uq_metric_snapshots_run_metric"),
        Index("ix_metric_snapshots_run", "evaluation_run_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    evaluation_run_id: Mapped[UUID] = mapped_column(nullable=False)
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_value: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False)
    # The bar this value was measured against; NULL for purely informational
    # metrics that do not participate in the gate.
    threshold: Mapped[float | None] = mapped_column(Numeric(12, 6))
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Extra per-metric context (per-class F1, channel recall, attack-kind coverage).
    dimensions_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EvaluationDetailView:
    """Read-side bundle: a run plus its ordered metric rows.

    Assembled by the repository rather than an ORM relationship, matching the
    ``reports``/``agent`` precedent — the in-memory and SQL adapters stay
    behaviour-identical because neither relies on lazy loading.
    """

    __slots__ = ("metrics", "run")

    def __init__(self, run: EvaluationRun, metrics: list[MetricSnapshot]) -> None:
        self.run = run
        self.metrics = metrics


__all__ = [
    "DatasetVersion",
    "EvaluationDetailView",
    "EvaluationKind",
    "EvaluationRun",
    "EvaluationStatus",
    "MetricSnapshot",
    "TERMINAL_EVALUATION_STATUSES",
    "is_terminal",
]
