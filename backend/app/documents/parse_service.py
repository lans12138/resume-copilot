"""Document parse task orchestration: status flow, idempotency, and retries.

This is the application service consumed by the Celery task wrapper. It owns the
resume document lifecycle for parsing:

    QUEUED -> PARSING -> REVIEW_REQUIRED   (parsed, awaiting extraction)
                    \\-> FAILED | UNSUPPORTED

Idempotency follows detailed design §14.1: ``operation_key = document_id +
parser_version + attempt``. Duplicate deliveries (same attempt) are skipped when
the document has already reached a terminal state; a newer attempt wins. The
database row lock plus the attempt/status guards make Celery at-least-once
delivery safe without relying on ``acks_late``.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from celery import Celery  # type: ignore[import-untyped]

from backend.app.core.errors import app_error
from backend.app.core.settings import Settings
from backend.app.documents.models import DocumentStatus, ResumeDocument
from backend.app.documents.parsers import (
    DocumentParseError,
    DocumentParser,
    DocxParagraphLocator,
    DocxTableLocator,
    ParsedBlock,
    ParsedDocument,
    ParseLimits,
    PdfLocator,
    PyMuPdfParser,
    PythonDocxParser,
)
from backend.app.documents.repository import DocumentRepository
from backend.app.documents.validation import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE
from backend.app.infrastructure.storage import StorageBackend


@runtime_checkable
class ParseEnqueuer(Protocol):
    """Delivers a parse task. Implemented by Celery in production, in-memory in tests."""

    def enqueue_parse(self, document_id: UUID, *, attempt: int, parser_version: str) -> None: ...


@runtime_checkable
class ProfileExtractionEnqueuer(Protocol):
    """Delivers the profile-extraction task that follows a successful parse.

    Kept as a port so the documents package never imports the candidates package:
    the Celery task wrapper supplies the concrete implementation (detailed design
    §7.4 — parsed blocks become a REVIEW_REQUIRED CandidateProfile draft).
    """

    def enqueue_extraction(self, document_id: UUID) -> None: ...


class TransientParseError(Exception):
    """Raised by ``process_parse`` for transient failures that Celery should retry."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


# Parse-stage error codes that must NOT be retried (detailed design §14.2).
_TERMINAL_PARSE_CODES = frozenset(
    {
        "INVALID_PDF",
        "ENCRYPTED_PDF",
        "INVALID_DOCX",
        "EMPTY_TEXT",
        "EXTRACTED_TEXT_LIMIT_EXCEEDED",
        "UNSUPPORTED_MEDIA",
        "FILE_TOO_LARGE",
    }
)


def build_parser_registry(*, parser_version: str) -> dict[str, DocumentParser]:
    return {
        PDF_MEDIA_TYPE: PyMuPdfParser(version=parser_version),
        DOCX_MEDIA_TYPE: PythonDocxParser(version=parser_version),
    }


def _parsed_to_dict(parsed: ParsedDocument) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    for block in parsed.blocks:
        locator = block.locator
        if isinstance(locator, PdfLocator):
            locator_dict: dict[str, Any] = {
                "kind": "pdf",
                "page_number": locator.page_number,
                "block_index": locator.block_index,
                "char_start": locator.char_start,
                "char_end": locator.char_end,
            }
        elif isinstance(locator, DocxParagraphLocator):
            locator_dict = {
                "kind": "docx_paragraph",
                "paragraph_index": locator.paragraph_index,
                "char_start": locator.char_start,
                "char_end": locator.char_end,
            }
        else:
            locator_dict = {
                "kind": "docx_table",
                "table_index": locator.table_index,
                "row_index": locator.row_index,
                "cell_index": locator.cell_index,
                "char_start": locator.char_start,
                "char_end": locator.char_end,
            }
        blocks.append(
            {"block_index": block.block_index, "text": block.text, "locator": locator_dict}
        )
    return {
        "media_type": parsed.media_type,
        "full_text": parsed.full_text,
        "blocks": blocks,
        "page_count": parsed.page_count,
        "paragraph_count": parsed.paragraph_count,
        "table_count": parsed.table_count,
        "warnings": list(parsed.warnings),
    }


def parsed_from_json(data: Mapping[str, Any]) -> ParsedDocument:
    """Rebuild a ``ParsedDocument`` from the stored ``ResumeDocument.parsed_json``.

    Public because the extraction task (candidates package) consumes the same
    persisted representation the parse stage wrote.
    """
    blocks: list[ParsedBlock] = []
    for raw in data["blocks"]:
        locator_dict = raw["locator"]
        kind = locator_dict["kind"]
        if kind == "pdf":
            locator: Any = PdfLocator(
                page_number=locator_dict["page_number"],
                block_index=locator_dict["block_index"],
                char_start=locator_dict["char_start"],
                char_end=locator_dict["char_end"],
            )
        elif kind == "docx_paragraph":
            locator = DocxParagraphLocator(
                paragraph_index=locator_dict["paragraph_index"],
                char_start=locator_dict["char_start"],
                char_end=locator_dict["char_end"],
            )
        else:
            locator = DocxTableLocator(
                table_index=locator_dict["table_index"],
                row_index=locator_dict["row_index"],
                cell_index=locator_dict["cell_index"],
                char_start=locator_dict["char_start"],
                char_end=locator_dict["char_end"],
            )
        blocks.append(
            ParsedBlock(block_index=raw["block_index"], text=raw["text"], locator=locator)
        )
    return ParsedDocument(
        media_type=data["media_type"],
        full_text=data["full_text"],
        blocks=tuple(blocks),
        page_count=data.get("page_count"),
        paragraph_count=data.get("paragraph_count"),
        table_count=data.get("table_count"),
        warnings=tuple(data.get("warnings", ())),
    )


class DocumentParseService:
    """Parse lifecycle for a single resume document (one worker task invocation)."""

    def __init__(
        self,
        *,
        documents: DocumentRepository,
        storage: StorageBackend,
        parsers: Mapping[str, DocumentParser],
        enqueue: ParseEnqueuer,
        settings: Settings,
        extract_profiles: ProfileExtractionEnqueuer | None = None,
    ) -> None:
        self._documents = documents
        self._storage = storage
        self._parsers = parsers
        self._enqueue = enqueue
        self._settings = settings
        self._extract_profiles = extract_profiles

    def _limits(self) -> ParseLimits:
        settings = self._settings
        return ParseLimits(
            max_pdf_pages=settings.max_pdf_pages,
            max_extracted_chars=settings.max_extracted_chars,
            timeout_seconds=settings.parser_timeout_seconds,
        )

    async def process_parse(
        self, document_id: UUID, *, attempt: int, parser_version: str
    ) -> DocumentStatus | None:
        document = await self._documents.get_for_update(document_id)
        if document is None:
            return None

        # Idempotency guards (operation_key = document_id + parser_version + attempt).
        # A never-parsed document has parser_version=None and must be allowed through.
        if document.parser_version is not None and document.parser_version != parser_version:
            return document.status  # parsed with a different version; ignore delivery.
        if document.attempt > attempt:
            return document.status  # a newer attempt already ran; duplicate ignored.
        if document.attempt == attempt and document.status in (
            DocumentStatus.REVIEW_REQUIRED,
            DocumentStatus.READY,
            DocumentStatus.FAILED,
            DocumentStatus.UNSUPPORTED,
        ):
            # Same attempt already terminal; a duplicate delivery must never
            # reopen a parsed document, and above all must never drag a
            # human-confirmed (READY) document back to REVIEW_REQUIRED.
            return document.status

        document.status = DocumentStatus.PARSING
        document.attempt = attempt
        document.error_code = None
        document.error_message_safe = None
        await self._documents.save(document)
        await self._documents.commit()

        parser = self._parsers.get(document.media_type)
        if parser is None:
            return await self._fail(
                document, "UNSUPPORTED_MEDIA", "没有适用于该文件类型的解析器", retryable=False
            )
        try:
            async with self._storage.open(document.storage_key) as stream:
                parsed = parser.parse(stream, self._limits())
        except DocumentParseError as error:
            if error.retryable:
                # Already committed PARSING; let Celery retry from scratch.
                raise TransientParseError(error.code, error.safe_message) from error
            return await self._fail(document, error.code, error.safe_message, retryable=False)
        except (OSError, RuntimeError):
            # Storage/IO failure is transient; let Celery retry from scratch.
            raise TransientParseError("STORAGE_UNAVAILABLE", "文件读取失败，请稍后重试") from None

        document.parsed_json = _parsed_to_dict(parsed)
        document.parser_version = parser_version
        document.status = DocumentStatus.REVIEW_REQUIRED
        document.retryable = False
        await self._documents.save(document)
        await self._documents.commit()
        # §7.4: the parse stage ends with a REVIEW_REQUIRED draft, so hand the
        # document to the extraction stage exactly once. A broker outage must not
        # fail a parse that already succeeded; the document stays REVIEW_REQUIRED
        # and is re-published by the maintenance sweep (FIN-006).
        if self._extract_profiles is not None:
            with suppress(Exception):
                self._extract_profiles.enqueue_extraction(document.id)
        return DocumentStatus.REVIEW_REQUIRED

    async def retry_parse(self, document_id: UUID) -> DocumentStatus:
        document = await self._documents.get_for_update(document_id)
        if document is None:
            raise app_error("DOCUMENT_NOT_FOUND", 404, "文档不存在")
        if document.status not in (DocumentStatus.FAILED, DocumentStatus.UNSUPPORTED):
            raise app_error(
                "NOT_RETRYABLE",
                http_status=409,
                safe_message="当前文档状态不可重试",
                details={"status": document.status.value},
            )

        next_attempt = document.attempt + 1
        try:
            self._enqueue.enqueue_parse(
                document.id, attempt=next_attempt, parser_version=self._settings.parser_version
            )
        except Exception as error:
            await self._documents.rollback()
            raise app_error(
                "ENQUEUE_FAILED",
                http_status=503,
                safe_message="无法投递重试任务",
                details={"reason": type(error).__name__},
                retryable=True,
            ) from error

        document.attempt = next_attempt
        document.status = DocumentStatus.QUEUED
        document.error_code = None
        document.error_message_safe = None
        document.retryable = False
        await self._documents.save(document)
        await self._documents.commit()
        return DocumentStatus.QUEUED

    async def _fail(
        self, document: ResumeDocument, code: str, message: str, *, retryable: bool
    ) -> DocumentStatus:
        document.status = (
            DocumentStatus.UNSUPPORTED if code == "UNSUPPORTED_MEDIA" else DocumentStatus.FAILED
        )
        document.error_code = code
        document.error_message_safe = message
        document.retryable = retryable
        await self._documents.save(document)
        await self._documents.commit()
        return document.status


@dataclass(slots=True)
class InMemoryDocumentRepository(DocumentRepository):
    """In-memory implementation used by unit tests (no PostgreSQL/Redis)."""

    _store: dict[UUID, ResumeDocument] = field(default_factory=dict)

    async def get_for_update(self, document_id: UUID) -> ResumeDocument | None:
        return self._store.get(document_id)

    async def save(self, document: ResumeDocument) -> None:
        self._store[document.id] = document

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class CeleryParseEnqueuer(ParseEnqueuer):
    """Production ``ParseEnqueuer`` that delivers a named Celery task."""

    def __init__(self, celery_app: Celery, task_name: str) -> None:
        self._celery_app = celery_app
        self._task_name = task_name

    def enqueue_parse(self, document_id: UUID, *, attempt: int, parser_version: str) -> None:
        self._celery_app.send_task(
            self._task_name,
            args=[str(document_id)],
            kwargs={"attempt": attempt, "parser_version": parser_version},
        )


class CeleryProfileExtractionEnqueuer(ProfileExtractionEnqueuer):
    """Production ``ProfileExtractionEnqueuer`` that delivers a named Celery task."""

    def __init__(self, celery_app: Celery, task_name: str) -> None:
        self._celery_app = celery_app
        self._task_name = task_name

    def enqueue_extraction(self, document_id: UUID) -> None:
        self._celery_app.send_task(self._task_name, args=[str(document_id)])
