"""Add parsed_json column to resume_documents.

Revision ID: 0007_add_parsed_json
Revises: 0006_create_evidence_chunks
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_add_parsed_json"
down_revision = "0006_create_evidence_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "resume_documents",
        sa.Column("parsed_json", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("resume_documents", "parsed_json")
