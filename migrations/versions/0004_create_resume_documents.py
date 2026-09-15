"""Create resume document upload metadata.

Revision ID: 0004_create_resume_documents
Revises: 0003_create_jobs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_create_resume_documents"
down_revision = "0003_create_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resume_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("storage_key", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retryable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message_safe", sa.String(500), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_resume_documents_size_positive"),
        sa.CheckConstraint(
            "status IN ('UPLOADED', 'QUEUED', 'PARSING', 'REVIEW_REQUIRED', 'READY', 'FAILED', 'UNSUPPORTED', 'SUPERSEDED')",
            name="ck_resume_documents_status",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"], ["users.id"], name="fk_resume_documents_uploaded_by_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_resume_documents"),
        sa.UniqueConstraint("content_sha256", name="uq_resume_documents_sha256"),
        sa.UniqueConstraint("storage_key", name="uq_resume_documents_storage_key"),
    )
    op.create_index(
        "ix_resume_documents_status_created", "resume_documents", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_resume_documents_status_created", table_name="resume_documents")
    op.drop_table("resume_documents")
