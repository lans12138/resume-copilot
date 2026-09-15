"""Create the authoritative user identity table.

Revision ID: 0002_create_users
Revises: 0001_enable_pgvector
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_create_users"
down_revision = "0001_enable_pgvector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
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
            "role IN ('HR', 'HIRING_MANAGER', 'ADMIN')",
            name="ck_users_role",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username_normalized"),
    )
    op.create_index("ix_users_role_active", "users", ["role", "is_active"])


def downgrade() -> None:
    op.drop_index("ix_users_role_active", table_name="users")
    op.drop_table("users")
