"""Enable the pgvector extension as the database foundation.

Revision ID: 0001_enable_pgvector
Revises: None
"""

from __future__ import annotations

from alembic import op

revision = "0001_enable_pgvector"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    # Keep the shared extension installed; dropping it can destroy future vector columns.
    pass
