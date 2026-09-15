"""Create evidence_chunks table with composite profile/document FK.

Revision ID: 0006_create_evidence_chunks
Revises: 0005_create_candidates
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_create_evidence_chunks"
down_revision = "0005_create_candidates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Vector extension backs the embedding column (filled by IMP-013). The MVP
    # keeps the column nullable and creates no HNSW index for exact-only search.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "evidence_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_profile_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("section_type", sa.String(length=64), nullable=False),
        sa.Column("locator_json", JSONB(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("embedding_model", sa.String(length=128), nullable=True),
        sa.Column("embedding_version", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["resume_documents.id"], name=op.f("fk_evidence_chunks_document_id")
        ),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"],
            ["candidate_profiles.id"],
            name=op.f("fk_evidence_chunks_candidate_profile_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_chunks")),
        sa.UniqueConstraint(
            "document_id", "chunk_index", name=op.f("uq_evidence_chunks_document_index")
        ),
    )
    # Composite FK binding a chunk to (profile_id, document_id) so cross-document
    # evidence is rejected at the constraint level, not just in app code.
    op.create_foreign_key(
        "fk_evidence_chunks_profile_document",
        "evidence_chunks",
        "candidate_profiles",
        ["candidate_profile_id", "document_id"],
        ["id", "document_id"],
    )
    op.create_index(
        "ix_evidence_chunks_profile_section",
        "evidence_chunks",
        ["candidate_profile_id", "section_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_chunks_profile_section", table_name="evidence_chunks")
    op.drop_constraint("fk_evidence_chunks_profile_document", "evidence_chunks", type_="foreignkey")
    op.drop_table("evidence_chunks")
    op.execute("DROP EXTENSION IF EXISTS vector")
