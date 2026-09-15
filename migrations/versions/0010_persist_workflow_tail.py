"""Persist ApplicationRun recovery state and workflow relationships.

Revision ID: 0010_persist_workflow_tail
Revises: 0009_link_applications
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_persist_workflow_tail"
down_revision = "0009_link_applications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_checkpoints",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_ns", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
        sa.Column("parent_id", sa.String(length=64), nullable=True),
        sa.Column("checkpoint_json", postgresql.JSONB(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["agent_runs.thread_id"],
            name="fk_agent_checkpoints_thread",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_checkpoints"),
        sa.UniqueConstraint(
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            name="uq_agent_checkpoints_identity",
        ),
    )
    op.create_index(
        "ix_agent_checkpoints_thread_latest",
        "agent_checkpoints",
        ["thread_id", "checkpoint_ns", "id"],
    )

    op.create_foreign_key(
        "fk_application_runs_match_report",
        "application_runs",
        "match_reports",
        ["match_report_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_application_status_history_run",
        "application_status_history",
        "application_runs",
        ["run_id"],
        ["run_id"],
    )
    op.create_foreign_key(
        "fk_application_status_history_approval",
        "application_status_history",
        "approvals",
        ["approval_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_approvals_id_run", "approvals", ["id", "application_run_id"]
    )
    op.create_foreign_key(
        "fk_interviews_run",
        "interviews",
        "application_runs",
        ["run_id"],
        ["run_id"],
    )
    op.create_foreign_key(
        "fk_interviews_approval_run",
        "interviews",
        "approvals",
        ["approval_id", "run_id"],
        ["id", "application_run_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_interviews_approval_run", "interviews", type_="foreignkey"
    )
    op.drop_constraint("fk_interviews_run", "interviews", type_="foreignkey")
    op.drop_constraint("uq_approvals_id_run", "approvals", type_="unique")
    op.drop_constraint(
        "fk_application_status_history_approval",
        "application_status_history",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_application_status_history_run",
        "application_status_history",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_application_runs_match_report",
        "application_runs",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_agent_checkpoints_thread_latest", table_name="agent_checkpoints"
    )
    op.drop_table("agent_checkpoints")
