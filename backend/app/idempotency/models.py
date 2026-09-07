"""Idempotency record aggregate (FIN-001).

A single ``IdempotencyRecord`` persists the outcome of one client request, keyed
by the caller-supplied ``Idempotency-Key`` header. The lifecycle is:

* ``IN_PROGRESS`` — claimed by the first request holding the key; concurrent
  requests with the same key block on it (or replay it once completed).
* ``COMPLETED`` — the response (status + body + resource reference) is frozen;
  any later identical request replays it instead of re-executing.
* ``FAILED`` — a transient failure happened; the key can be taken over on retry.

The record lives in its own short transaction(s), decoupled from the business
transaction. The business operations it guards (approval decide, run cancel/
retry) are themselves idempotent, so even if the COMPLETED write is lost the
underlying side effect is never double-applied (FIN-001 defense in depth).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database import Base


class IdempotencyStatus(StrEnum):
    """Processing state of a single idempotency key (§20.3 FIN-001)."""

    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class IdempotencyRecord(Base):
    """One client request outcome, keyed by ``Idempotency-Key``."""

    __tablename__ = "idempotency_records"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Client-supplied key; the unique index is the concurrency backstop.
    key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    # Stable per-endpoint label derived from the route path.
    operation: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # sha256(method|path|query|body); distinguishes a reused key on a different request.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[IdempotencyStatus] = mapped_column(
        String(16), default=IdempotencyStatus.IN_PROGRESS, nullable=False
    )
    # Frozen response for replay.
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Reference to the created/changed resource (e.g. approval id).
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Debug summary: "POST /api/v1/approvals/{id}/decision".
    request_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Set on every claim/takeover; drives stale-lock detection (crash/restart).
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Lifecycle retention for completed records.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", onupdate="now()", nullable=False
    )
