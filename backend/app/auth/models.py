"""Persistent user identity model."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database import Base


class UserRole(StrEnum):
    HR = "HR"
    HIRING_MANAGER = "HIRING_MANAGER"
    ADMIN = "ADMIN"


class User(Base):
    """Authoritative account state used for every authenticated request."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('HR', 'HIRING_MANAGER', 'ADMIN')",
            name="ck_users_role",
        ),
        UniqueConstraint("username", name="uq_users_username_normalized"),
        Index("ix_users_role_active", "role", "is_active"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="ck_users_role",
            native_enum=False,
            create_constraint=False,
            values_callable=lambda roles: [role.value for role in roles],
            length=32,
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
