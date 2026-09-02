"""Public resume upload response schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel

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
