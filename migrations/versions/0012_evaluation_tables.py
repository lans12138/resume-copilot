"""Create dataset_versions, evaluation_runs and metric_snapshots (FIN-007).

Detailed design §4.6 / §4.7 migration step 8. These three tables make an offline
gate verdict durable and auditable: what was evaluated, how, and with what
result. Finished runs are immutable, so the schema deliberately has no
"updated metrics" path — a re-run inserts a new ``evaluation_runs`` row.

Revision ID: 0012_evaluation_tables
Revises: 0011_idempotency_records
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_evaluation_tables"
down_revision = "0011_idempotency_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("manifest_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dataset_versions"),
        sa.UniqueConstraint("name", "version", name="uq_dataset_versions_name_version"),
        sa.CheckConstraint(
            "length(content_hash) > 0",
            name="ck_dataset_versions_hash_nonempty",
        ),
    )
    op.create_index(
        "ix_dataset_versions_name_version", "dataset_versions", ["name", "version"]
    )

    op.create_table(
        "evaluation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("model_snapshot_json", postgresql.JSONB(), nullable=False),
        sa.Column("prompt_versions_json", postgresql.JSONB(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message_safe", sa.String(length=500), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_runs"),
        sa.ForeignKeyConstraint(
            ["dataset_version_id"],
            ["dataset_versions.id"],
            name="fk_evaluation_runs_dataset",
        ),
        sa.CheckConstraint(
            "status IN ('CREATED','RUNNING','COMPLETED','FAILED')",
            name="ck_evaluation_runs_status",
        ),
        sa.CheckConstraint(
            "kind IN ('GOLDEN','SEMANTIC','INJECTION')",
            name="ck_evaluation_runs_kind",
        ),
    )
    op.create_index("ix_evaluation_runs_status", "evaluation_runs", ["status"])
    op.create_index(
        "ix_evaluation_runs_dataset", "evaluation_runs", ["dataset_version_id"]
    )
    op.create_index(
        "ix_evaluation_runs_dedupe",
        "evaluation_runs",
        ["dataset_version_id", "config_hash", "status"],
    )

    op.create_table(
        "metric_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evaluation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("metric_name", sa.String(length=128), nullable=False),
        sa.Column("metric_value", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("threshold", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("dimensions_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_metric_snapshots"),
        sa.ForeignKeyConstraint(
            ["evaluation_run_id"],
            ["evaluation_runs.id"],
            name="fk_metric_snapshots_run",
        ),
        sa.UniqueConstraint(
            "evaluation_run_id",
            "metric_name",
            name="uq_metric_snapshots_run_metric",
        ),
    )
    op.create_index(
        "ix_metric_snapshots_run", "metric_snapshots", ["evaluation_run_id"]
    )


def downgrade() -> None:
    # Child first: metric_snapshots references evaluation_runs.
    op.drop_index("ix_metric_snapshots_run", table_name="metric_snapshots")
    op.drop_table("metric_snapshots")
    op.drop_index("ix_evaluation_runs_dedupe", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_dataset", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_status", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_index("ix_dataset_versions_name_version", table_name="dataset_versions")
    op.drop_table("dataset_versions")
