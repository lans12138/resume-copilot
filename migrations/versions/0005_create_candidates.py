"""Create candidates and candidate_profiles tables.

Revision ID: 0005_create_candidates
Revises: 0004_create_resume_documents
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0005_create_candidates"
down_revision = "0004_create_resume_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("normalized_email_hash", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("char_length(display_name) > 0", name="ck_candidates_name_non_empty"),
        sa.PrimaryKeyConstraint("id", name="pk_candidates"),
    )

    op.create_table(
        "candidate_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("profile_json", JSONB(), nullable=False),
        sa.Column("normalized_skills", ARRAY(sa.Text()), nullable=False),
        sa.Column("years_experience", sa.Numeric(5, 2), nullable=True),
        sa.Column("education_level", sa.String(64), nullable=True),
        sa.Column("schema_version", sa.String(32), server_default="v1", nullable=False),
        sa.Column("confirmed_by", sa.Uuid(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'REVIEW_REQUIRED', 'READY', 'SUPERSEDED')",
            name="ck_candidate_profiles_status",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidates.id"], name="fk_candidate_profiles_candidate"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["resume_documents.id"], name="fk_candidate_profiles_document"
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by"], ["users.id"], name="fk_candidate_profiles_confirmed_by"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_candidate_profiles"),
        sa.UniqueConstraint(
            "candidate_id", "version_no", name="uq_candidate_profiles_candidate_version"
        ),
        sa.UniqueConstraint("id", "document_id", name="uq_candidate_profiles_id_document"),
    )
    op.create_index("ix_candidate_profiles_document", "candidate_profiles", ["document_id"])
    op.create_index(
        "uq_candidate_profiles_ready",
        "candidate_profiles",
        ["candidate_id"],
        unique=True,
        postgresql_where=sa.text("status = 'READY'"),
    )


def downgrade() -> None:
    op.drop_index("uq_candidate_profiles_ready", table_name="candidate_profiles")
    op.drop_index("ix_candidate_profiles_document", table_name="candidate_profiles")
    op.drop_table("candidate_profiles")
    op.drop_table("candidates")
