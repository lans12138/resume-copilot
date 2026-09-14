"""Public resume upload response schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from backend.app.documents.models import DocumentStatus


class UploadOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


class DocumentResponse(BaseModel):
    id: UUID
    original_filename: str
    media_type: str
    size_bytes: int
    content_sha256: str
    status: DocumentStatus
    parser_version: str | None
    attempt: int
    retryable: bool
    error_code: str | None
    error_message: str | None
    uploaded_by: UUID
    created_at: datetime
    updated_at: datetime


class UploadError(BaseModel):
    code: str
    message: str
    http_status: int


class DocumentUploadResult(BaseModel):
    filename: str
    outcome: UploadOutcome
    resource_id: UUID | None = None
    status_url: str | None = None
    document: DocumentResponse | None = None
    duplicate_of: UUID | None = None
    error: UploadError | None = None


class DocumentBatchAccepted(BaseModel):
    items: list[DocumentUploadResult]
    total: int
    accepted: int
    duplicates: int
    rejected: int


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    page: int
    page_size: int
    total: int


class ParsedBlockView(BaseModel):
    """One parsed block with the locator that maps it back to the source file."""

    block_index: int
    text: str
    locator: dict[str, Any]


class DocumentContentResponse(BaseModel):
    """Parsed source content behind a document.

    This is what the profile review screen renders next to an evidence chunk:
    ``blocks[i].text`` is the原文 a locator points at, and ``locator`` carries the
    page/paragraph/char range so the UI can highlight the exact excerpt a claim
    was pinned to. Read-only and HR-only; the payload is the parser's own
    serialization (see ``parsed_to_json``) re-validated on read.
    """

    document_id: UUID
    media_type: str
    full_text: str
    page_count: int | None = None
    paragraph_count: int | None = None
    table_count: int | None = None
    warnings: list[str] = Field(default_factory=list)
    blocks: list[ParsedBlockView]
