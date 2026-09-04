"""Candidate aggregate persistence models.

A ``Candidate`` is the person subject (a synthetic record in MVP). A
``CandidateProfile`` is one extracted, versioned, and eventually human-confirmed
view of that person sourced from a single resume document. Only the READY
version is eligible for retrieval; earlier READY versions are superseded on
confirmation of a newer draft, enforced by a partial unique index.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database import Base


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        CheckConstraint("char_length(display_name) > 0", name="ck_candidates_name_non_empty"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_email_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CandidateProfileStatus(StrEnum):
    DRAFT = "DRAFT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    READY = "READY"
    SUPERSEDED = "SUPERSEDED"


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'REVIEW_REQUIRED', 'READY', 'SUPERSEDED')",
            name="ck_candidate_profiles_status",
        ),
        UniqueConstraint(
            "candidate_id", "version_no", name="uq_candidate_profiles_candidate_version"
        ),
        # Composite identity referenced by evidence_chunks (IMP-011) so a chunk is
        # provably bound to (profile_id, document_id).
        UniqueConstraint("id", "document_id", name="uq_candidate_profiles_id_document"),
        Index("ix_candidate_profiles_document", "document_id"),
        # Only one READY profile per candidate at any time.
        Index(
            "uq_candidate_profiles_ready",
            "candidate_id",
            unique=True,
            postgresql_where=text("status = 'READY'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("candidates.id"), nullable=False)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("resume_documents.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[CandidateProfileStatus] = mapped_column(
        String(32), nullable=False, default=CandidateProfileStatus.DRAFT
    )
    profile_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    normalized_skills: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    years_experience: Mapped[float | None] = mapped_column(Numeric(5, 2))
    education_level: Mapped[str | None] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(32), default="v1", nullable=False)
    confirmed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}


class EvidenceChunk(Base):
    """A verbatim, source-pinned excerpt backing a deterministic claim.

    The composite FK ``(candidate_profile_id, document_id)`` resolves to
    ``candidate_profiles(id, document_id)`` (the ``uq_candidate_profiles_id_document``
    unique pair). Because that pair is unique, a chunk can only reference a profile
    together with the exact document that profile was extracted from — a chunk from a
    different document is rejected at the FK boundary (cross-document evidence).
    """

    __tablename__ = "evidence_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_evidence_chunks_document_index"
        ),
        ForeignKeyConstraint(
            ["candidate_profile_id", "document_id"],
            ["candidate_profiles.id", "candidate_profiles.document_id"],
            name="fk_evidence_chunks_profile_document",
        ),
        Index("ix_evidence_chunks_profile_section", "candidate_profile_id", "section_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("resume_documents.id"), nullable=False
    )
    candidate_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_profiles.id"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    section_type: Mapped[str] = mapped_column(String(64), nullable=False)
    locator_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    embedding_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
