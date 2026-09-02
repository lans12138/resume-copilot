"""Resume document upload metadata and processing lifecycle."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database import Base


class DocumentStatus(StrEnum):
    UPLOADED = "UPLOADED"
    QUEUED = "QUEUED"
    PARSING = "PARSING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    READY = "READY"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    SUPERSEDED = "SUPERSEDED"


class ResumeDocument(Base):
    __tablename__ = "resume_documents"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="ck_resume_documents_size_positive"),
        CheckConstraint(
            "status IN ('UPLOADED', 'QUEUED', 'PARSING', 'REVIEW_REQUIRED', "
            "'READY', 'FAILED', 'UNSUPPORTED', 'SUPERSEDED')",
            name="ck_resume_documents_status",
        ),
        Index("ix_resume_documents_status_created", "status", "created_at"),
        UniqueConstraint("storage_key", name="uq_resume_documents_storage_key"),
        UniqueConstraint("content_sha256", name="uq_resume_documents_sha256"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(String(32), nullable=False)
    parser_version: Mapped[str | None] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    retryable: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message_safe: Mapped[str | None] = mapped_column(String(500))
    uploaded_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
