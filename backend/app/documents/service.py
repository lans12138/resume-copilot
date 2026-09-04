"""Per-file resume upload transaction and duplicate handling."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError
from backend.app.documents.models import DocumentStatus, ResumeDocument
from backend.app.documents.parse_service import ParseEnqueuer
from backend.app.documents.schemas import (
    DocumentBatchAccepted,
    DocumentListResponse,
    DocumentResponse,
    DocumentUploadResult,
    UploadError,
    UploadOutcome,
)
from backend.app.documents.validation import UploadRejected, validate_upload
from backend.app.infrastructure.storage import StorageBackend, StorageLimitExceeded


def document_response(document: ResumeDocument) -> DocumentResponse:
    return DocumentResponse(
        id=document.id,
        original_filename=document.original_filename,
        media_type=document.media_type,
        size_bytes=document.size_bytes,
        content_sha256=document.content_sha256,
        status=document.status,
        parser_version=document.parser_version,
        attempt=document.attempt,
        retryable=document.retryable,
        error_code=document.error_code,
        error_message=document.error_message_safe,
        uploaded_by=document.uploaded_by,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


class DocumentUploadService:
    def __init__(
        self,
        session: AsyncSession,
        storage: StorageBackend,
        *,
        max_file_size_bytes: int,
        parser_version: str,
        enqueue: ParseEnqueuer | None = None,
    ) -> None:
        self.session = session
        self.storage = storage
        self.max_file_size_bytes = max_file_size_bytes
        self.parser_version = parser_version
        self.enqueue = enqueue

    async def upload_batch(
        self, actor: Actor, uploads: Sequence[UploadFile]
    ) -> DocumentBatchAccepted:
        self._require_hr(actor)
        items = [await self._upload_one(actor, upload) for upload in uploads]
        for item in items:
            if item.outcome is UploadOutcome.ACCEPTED and item.resource_id is not None:
                await self._enqueue_parse(item.resource_id)
        return DocumentBatchAccepted(
            items=items,
            total=len(items),
            accepted=sum(item.outcome is UploadOutcome.ACCEPTED for item in items),
            duplicates=sum(item.outcome is UploadOutcome.DUPLICATE for item in items),
            rejected=sum(item.outcome is UploadOutcome.REJECTED for item in items),
        )

    async def _enqueue_parse(self, document_id: UUID) -> None:
        document = await self.session.get(ResumeDocument, document_id)
        if document is None:
            return
        # §7.2 step 6: persist QUEUED, then deliver the parse task after commit.
        document.status = DocumentStatus.QUEUED
        document.attempt = 1
        await self.session.commit()
        if self.enqueue is None:
            return
        try:
            self.enqueue.enqueue_parse(document.id, attempt=1, parser_version=self.parser_version)
        except Exception:
            document.status = DocumentStatus.UPLOADED
            await self.session.commit()

    async def _upload_one(self, actor: Actor, upload: UploadFile) -> DocumentUploadResult:
        display_name = _display_filename(upload.filename)
        try:
            validated = validate_upload(
                upload.file,
                filename=upload.filename,
                declared_media_type=upload.content_type,
                declared_size=upload.size,
                max_size_bytes=self.max_file_size_bytes,
            )
            stored = await self.storage.put(upload.file, media_type=validated.media_type)
        except UploadRejected as error:
            return _rejected(display_name, error.code, error.safe_message)
        except StorageLimitExceeded:
            return _rejected(display_name, "FILE_TOO_LARGE", "文件超过允许的大小")
        except ValueError:
            return _rejected(display_name, "EMPTY_FILE", "不接受空文件")

        existing = await self._find_duplicate(stored.content_sha256)
        if existing is not None:
            await self.storage.delete_if_unreferenced(stored.storage_key)
            return DocumentUploadResult(
                filename=validated.original_filename,
                outcome=UploadOutcome.DUPLICATE,
                resource_id=existing.id,
                status_url=f"/api/v1/documents/{existing.id}",
                document=document_response(existing),
                duplicate_of=existing.id,
            )

        document = ResumeDocument(
            original_filename=validated.original_filename,
            storage_key=stored.storage_key,
            media_type=stored.media_type,
            size_bytes=stored.size_bytes,
            content_sha256=stored.content_sha256,
            status=DocumentStatus.UPLOADED,
            uploaded_by=actor.user_id,
        )
        self.session.add(document)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            duplicate = await self._find_duplicate(stored.content_sha256)
            await self.storage.delete_if_unreferenced(stored.storage_key)
            if duplicate is None:
                raise
            return DocumentUploadResult(
                filename=validated.original_filename,
                outcome=UploadOutcome.DUPLICATE,
                resource_id=duplicate.id,
                status_url=f"/api/v1/documents/{duplicate.id}",
                document=document_response(duplicate),
                duplicate_of=duplicate.id,
            )
        except Exception:
            await self.session.rollback()
            await self.storage.delete_if_unreferenced(stored.storage_key)
            raise
        return DocumentUploadResult(
            filename=validated.original_filename,
            outcome=UploadOutcome.ACCEPTED,
            resource_id=document.id,
            status_url=f"/api/v1/documents/{document.id}",
            document=document_response(document),
        )

    async def list_documents(
        self,
        actor: Actor,
        *,
        page: int,
        page_size: int,
        status: DocumentStatus | None,
    ) -> DocumentListResponse:
        self._require_hr(actor)
        filters = () if status is None else (ResumeDocument.status == status,)
        total = await self.session.scalar(
            select(func.count()).select_from(ResumeDocument).where(*filters)
        )
        rows = await self.session.scalars(
            select(ResumeDocument)
            .where(*filters)
            .order_by(ResumeDocument.created_at.desc(), ResumeDocument.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return DocumentListResponse(
            items=[document_response(item) for item in rows],
            page=page,
            page_size=page_size,
            total=int(total or 0),
        )

    async def get_document(self, actor: Actor, document_id: UUID) -> DocumentResponse:
        self._require_hr(actor)
        document = await self.session.get(ResumeDocument, document_id)
        if document is None:
            raise AppError(
                code="DOCUMENT_NOT_FOUND",
                http_status=404,
                safe_message="文档不存在",
            )
        return document_response(document)

    async def _find_duplicate(self, digest: str) -> ResumeDocument | None:
        document: ResumeDocument | None = await self.session.scalar(
            select(ResumeDocument).where(ResumeDocument.content_sha256 == digest)
        )
        return document

    @staticmethod
    def _require_hr(actor: Actor) -> None:
        if actor.role is not UserRole.HR:
            raise AppError(
                code="FORBIDDEN",
                http_status=403,
                safe_message="当前用户无简历访问权限",
            )


def _display_filename(filename: str | None) -> str:
    value = (filename or "unnamed").replace("\\", "/").split("/")[-1]
    value = "".join(char for char in value if ord(char) >= 32).strip()
    return (value or "unnamed")[:255]


def _rejected(filename: str, code: str, message: str) -> DocumentUploadResult:
    if code == "FILE_TOO_LARGE":
        http_status = 413
    elif code in {
        "UNSUPPORTED_MEDIA",
        "SIGNATURE_MISMATCH",
        "INVALID_DOCX",
        "ENCRYPTED_ARCHIVE",
        "UNSAFE_ARCHIVE_PATH",
        "UNSAFE_DOCX_CONTENT",
        "ARCHIVE_LIMIT_EXCEEDED",
    }:
        http_status = 415
    else:
        http_status = 422
    return DocumentUploadResult(
        filename=filename,
        outcome=UploadOutcome.REJECTED,
        error=UploadError(code=code, message=message, http_status=http_status),
    )
