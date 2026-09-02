"""Create jobs, immutable versions, and revocable assignments.

Revision ID: 0003_create_jobs
Revises: 0002_create_users
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_create_jobs"
down_revision = "0002_create_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'CLOSED')", name="ck_jobs_status"),
        sa.CheckConstraint(
            "status = 'DRAFT' OR current_version_id IS NOT NULL",
            name="ck_jobs_published_version",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_jobs_created_by_users"),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
    )
    op.create_index("ix_jobs_status_created_at", "jobs", ["status", "created_at"])
    op.create_table(
        "job_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("description_text", sa.Text(), nullable=False),
        sa.Column("requirements_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_job_versions_created_by_users"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_job_versions_job_id_jobs"),
        sa.PrimaryKeyConstraint("id", name="pk_job_versions"),
        sa.UniqueConstraint("id", "job_id", name="uq_job_versions_id_job"),
        sa.UniqueConstraint("job_id", "content_sha256", name="uq_job_versions_job_hash"),
        sa.UniqueConstraint("job_id", "version_no", name="uq_job_versions_job_no"),
    )
    op.create_foreign_key(
        "fk_jobs_current_version_belongs_to_job",
        "jobs",
        "job_versions",
        ["current_version_id", "id"],
        ["id", "job_id"],
    )
    op.create_table(
        "job_assignments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_by", sa.Uuid(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["assigned_by"], ["users.id"], name="fk_assignments_assigned_by_users"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_assignments_job_id_jobs"),
        sa.ForeignKeyConstraint(["revoked_by"], ["users.id"], name="fk_assignments_revoked_by_users"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_assignments_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_job_assignments"),
    )
    op.create_index(
        "uq_job_assignments_active",
        "job_assignments",
        ["job_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_job_assignments_active", table_name="job_assignments")
    op.drop_table("job_assignments")
    op.drop_constraint("fk_jobs_current_version_belongs_to_job", "jobs", type_="foreignkey")
    op.drop_table("job_versions")
    op.drop_index("ix_jobs_status_created_at", table_name="jobs")
    op.drop_table("jobs")
