"""Add match_explanations and report_claims.source (PORT-003).

Two changes, one reason: a report now carries model-authored conclusions
alongside the deterministic ones, and the two must be tellable apart.

``report_claims.source`` distinguishes a deterministic verdict (``RULE`` — the
only thing that may move a candidate out of the shortlist, BR-002) from
explanatory model commentary (``MODEL``, BR-001). It is added with a server
default of ``'RULE'`` because every claim written before this revision *is* a
deterministic one; a migration that had to infer the value could get it wrong,
and a wrong ``source`` on a historical claim would relabel a decision as
commentary.

``match_explanations`` records one model call per candidate report: status, reason
code, model and prompt/rule versions, latency, attempts and any token usage the
provider reported. It is keyed by ``(run_id, application_id)`` rather than by
``report_id`` so the report aggregate stays independently replaceable on a retry
— see ``explanations.models`` for the full argument.

No backfill is needed or wanted: an existing report has no model call to record,
and inventing ``UNAVAILABLE`` rows would claim those runs had been explained.

Revision ID: 0013_match_explanations
Revises: 0012_evaluation_tables
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_match_explanations"
down_revision = "0012_evaluation_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``server_default`` is what makes this safe on a populated table: existing
    # rows are relabelled RULE by the database, not by a guess in Python.
    op.add_column(
        "report_claims",
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="RULE",
        ),
    )
    op.create_check_constraint(
        "ck_report_claims_source",
        "report_claims",
        "source IN ('RULE','MODEL')",
    )

    op.create_table(
        "match_explanations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("conclusion_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "dropped_conclusion_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "illegal_citation_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_match_explanations"),
        sa.UniqueConstraint(
            "run_id",
            "application_id",
            name="uq_match_explanations_run_application",
        ),
        sa.CheckConstraint(
            "status IN ('SUCCEEDED','UNAVAILABLE')",
            name="ck_match_explanations_status",
        ),
        sa.CheckConstraint(
            "latency_ms >= 0", name="ck_match_explanations_latency_nonneg"
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_match_explanations_attempts_nonneg"
        ),
        sa.CheckConstraint(
            "conclusion_count >= 0 AND dropped_conclusion_count >= 0 "
            "AND illegal_citation_count >= 0",
            name="ck_match_explanations_counts_nonneg",
        ),
        sa.CheckConstraint(
            "dropped_conclusion_count <= conclusion_count",
            name="ck_match_explanations_dropped_lte_total",
        ),
    )
    op.create_index(
        "ix_match_explanations_run", "match_explanations", ["run_id"]
    )
    op.create_index(
        "ix_match_explanations_status", "match_explanations", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_match_explanations_status", table_name="match_explanations")
    op.drop_index("ix_match_explanations_run", table_name="match_explanations")
    op.drop_table("match_explanations")
    op.drop_constraint("ck_report_claims_source", "report_claims", type_="check")
    op.drop_column("report_claims", "source")
