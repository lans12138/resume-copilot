"""Create run, report, approval, and interview workflow tables.

Revision ID: 0008_create_workflow_tables
Revises: 0007_add_parsed_json
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_create_workflow_tables"
down_revision = "0007_add_parsed_json"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("run_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("next_event_sequence", sa.Integer(), nullable=False),
        sa.Column("config_snapshot_json", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_by", sa.Uuid(), nullable=True),
        sa.Column(
            "retryable",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message_safe", sa.String(length=500), nullable=True),
        sa.Column("failed_node", sa.String(length=128), nullable=True),
        sa.Column(
            "snapshot_version",
            sa.String(length=64),
            server_default=sa.text("'v1'"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt >= 1", name="ck_agent_runs_attempt"),
        sa.CheckConstraint("next_event_sequence >= 0", name="ck_agent_runs_seq"),
        sa.CheckConstraint(
            "status IN ('CREATED','RUNNING','WAITING_APPROVAL','INTERRUPTED',"
            "'COMPLETED','FAILED','CANCELLED')",
            name="ck_agent_runs_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
        sa.UniqueConstraint("thread_id"),
    )
    op.create_index("ix_agent_runs_thread_id", "agent_runs", ["thread_id"])

    op.create_table(
        "agent_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("run_type", sa.String(length=32), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("node", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message_key", sa.String(length=128), nullable=False),
        sa.Column("safe_payload_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('RUN_CREATED','NODE_STARTED','NODE_COMPLETED','RUN_RESUMED',"
            "'STATUS_CHANGED','RUN_COMPLETED','RUN_FAILED','RUN_CANCELLED')",
            name="ck_agent_events_event_type",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], name="fk_agent_events_run"),
        sa.PrimaryKeyConstraint("id", name="pk_agent_events"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_agent_events_run_sequence"),
    )
    op.create_index(
        "ix_agent_events_run_sequence", "agent_events", ["run_id", "sequence"]
    )

    op.create_table(
        "job_applications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("active_application_run_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
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
        sa.CheckConstraint(
            "status IN ('CREATED','SHORTLISTED','ON_HOLD','REJECTED',"
            "'INTERVIEW_SCHEDULED')",
            name="ck_job_applications_status",
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_job_applications"),
        sa.UniqueConstraint(
            "job_id", "candidate_id", name="uq_job_applications_job_candidate"
        ),
    )
    op.create_index(
        "ix_job_applications_job_status", "job_applications", ["job_id", "status"]
    )
    op.create_index(
        "uq_job_applications_active_run",
        "job_applications",
        ["active_application_run_id"],
        unique=True,
        postgresql_where=sa.text("active_application_run_id IS NOT NULL"),
    )

    op.create_table(
        "match_runs",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("job_version_id", sa.Uuid(), nullable=False),
        sa.Column("retrieval_config_json", postgresql.JSONB(), nullable=False),
        sa.Column("model_config_json", postgresql.JSONB(), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_match_runs_job"),
        sa.ForeignKeyConstraint(
            ["job_version_id"], ["job_versions.id"], name="fk_match_runs_job_version"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], name="fk_match_runs_run"),
        sa.PrimaryKeyConstraint("run_id", name="pk_match_runs"),
    )
    op.create_index("ix_match_runs_job", "match_runs", ["job_id"])

    op.create_table(
        "match_run_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_profile_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_order", sa.Integer(), nullable=False),
        sa.Column("structured_rank", sa.Integer(), nullable=True),
        sa.Column("keyword_rank", sa.Integer(), nullable=True),
        sa.Column("vector_rank", sa.Integer(), nullable=True),
        sa.Column("structured_score", sa.Numeric(precision=18, scale=10), nullable=True),
        sa.Column("keyword_score", sa.Numeric(precision=18, scale=10), nullable=True),
        sa.Column("vector_score", sa.Numeric(precision=18, scale=10), nullable=True),
        sa.Column("rrf_score", sa.Numeric(precision=18, scale=10), nullable=False),
        sa.Column("hard_rule_result_json", postgresql.JSONB(), nullable=True),
        sa.Column("processing_status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "processing_status IN ('PENDING','COMPLETED','FAILED')",
            name="ck_match_run_candidates_status",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"],
            ["candidate_profiles.id"],
            name="fk_match_run_candidates_profile",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["match_runs.run_id"], name="fk_match_run_candidates_run"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_match_run_candidates"),
        sa.UniqueConstraint(
            "run_id", "snapshot_order", name="uq_match_run_candidates_order"
        ),
        sa.UniqueConstraint(
            "run_id", "candidate_profile_id", name="uq_match_run_candidates_profile"
        ),
    )
    op.create_index(
        "ix_match_run_candidates_run", "match_run_candidates", ["run_id"]
    )

    op.create_table(
        "match_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_profile_id", sa.Uuid(), nullable=False),
        sa.Column("overall_score", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("recommendation", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("model_snapshot_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["match_runs.run_id"], name="fk_match_reports_run"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_match_reports"),
        sa.UniqueConstraint(
            "run_id", "application_id", name="uq_match_reports_run_application"
        ),
    )
    op.create_index("ix_match_reports_run", "match_reports", ["run_id"])

    op.create_table(
        "report_claims",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("claim_type", sa.String(length=64), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("impact_level", sa.String(length=16), nullable=False),
        sa.Column("support_level", sa.String(length=16), nullable=False),
        sa.Column("confidence_note", sa.Text(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["report_id"], ["match_reports.id"], name="fk_report_claims_report"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_report_claims"),
        sa.UniqueConstraint("report_id", "display_order", name="uq_report_claims_order"),
    )
    op.create_index("ix_report_claims_report", "report_claims", ["report_id"])

    op.create_table(
        "claim_evidences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_chunk_id", sa.Uuid(), nullable=False),
        sa.Column("quote_text", sa.Text(), nullable=False),
        sa.Column("quote_start", sa.Integer(), nullable=False),
        sa.Column("quote_end", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "quote_end > quote_start", name="ck_claim_evidences_end_gt_start"
        ),
        sa.CheckConstraint(
            "quote_start >= 0", name="ck_claim_evidences_start_nonneg"
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["report_claims.id"], name="fk_claim_evidences_claim"
        ),
        sa.ForeignKeyConstraint(
            ["evidence_chunk_id"],
            ["evidence_chunks.id"],
            name="fk_claim_evidences_chunk",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_claim_evidences"),
        sa.UniqueConstraint(
            "claim_id",
            "evidence_chunk_id",
            "quote_start",
            "quote_end",
            name="uq_claim_evidences_claim_chunk_range",
        ),
    )

    op.create_table(
        "application_runs",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("match_report_id", sa.Uuid(), nullable=True),
        sa.Column("completion_reason", sa.String(length=64), nullable=True),
        sa.Column("question_set_json", postgresql.JSONB(), nullable=True),
        sa.Column("question_schema_version", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["job_applications.id"],
            name="fk_application_runs_application",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["agent_runs.id"], name="fk_application_runs_run"
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_application_runs"),
        sa.UniqueConstraint(
            "run_id", "application_id", name="uq_application_runs_run_application"
        ),
    )
    op.create_index(
        "ix_application_runs_application", "application_runs", ["application_id"]
    )
    op.create_foreign_key(
        "fk_job_applications_active_run",
        "job_applications",
        "application_runs",
        ["active_application_run_id", "id"],
        ["run_id", "application_id"],
    )

    op.create_table(
        "application_status_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=False),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("safe_reason", sa.String(length=256), nullable=True),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["application_id"], ["job_applications.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_application_status_history"),
    )
    op.create_index(
        "ix_application_status_history_application",
        "application_status_history",
        ["application_id"],
    )
    op.create_index(
        "ix_application_status_history_approval",
        "application_status_history",
        ["approval_id"],
    )

    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_run_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("original_params_json", postgresql.JSONB(), nullable=True),
        sa.Column("final_params_json", postgresql.JSONB(), nullable=True),
        sa.Column("expected_application_version", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expiration_reason", sa.String(length=32), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_result_json", postgresql.JSONB(), nullable=True),
        sa.Column("execution_error_code", sa.String(length=64), nullable=True),
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
        sa.CheckConstraint(
            "action_type IN ('UPDATE_APPLICATION_STATUS','CREATE_INTERVIEW_SCHEDULE')",
            name="ck_approvals_action_type",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','APPROVED','EDITED','REJECTED','EXECUTED',"
            "'EXECUTION_FAILED','EXPIRED')",
            name="ck_approvals_status",
        ),
        sa.ForeignKeyConstraint(
            ["application_run_id"],
            ["application_runs.run_id"],
            name="fk_approvals_application_run",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
        sa.UniqueConstraint("idempotency_key", name="uq_approvals_idempotency_key"),
    )
    op.create_index(
        "ix_approvals_pending_expiry", "approvals", ["status", "expires_at"]
    )
    op.create_index(
        "uq_approvals_pending_run",
        "approvals",
        ["application_run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    op.create_table(
        "interviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("external_schedule_id", sa.String(length=128), nullable=False),
        sa.Column("schedule_json", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
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
        sa.CheckConstraint(
            "external_schedule_id IS NOT NULL", name="ck_interviews_external_id"
        ),
        sa.CheckConstraint(
            "status IN ('SCHEDULED','CANCELLED')", name="ck_interviews_status"
        ),
        sa.ForeignKeyConstraint(["application_id"], ["job_applications.id"]),
        sa.PrimaryKeyConstraint("id", name="pk_interviews"),
        sa.UniqueConstraint("external_schedule_id"),
    )


def downgrade() -> None:
    op.drop_table("interviews")
    op.drop_table("approvals")
    op.drop_table("application_status_history")
    op.drop_constraint(
        "fk_job_applications_active_run", "job_applications", type_="foreignkey"
    )
    op.drop_table("application_runs")
    op.drop_table("claim_evidences")
    op.drop_table("report_claims")
    op.drop_table("match_reports")
    op.drop_table("match_run_candidates")
    op.drop_table("match_runs")
    op.drop_table("job_applications")
    op.drop_table("agent_events")
    op.drop_table("agent_runs")
