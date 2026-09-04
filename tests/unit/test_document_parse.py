"""Unit tests for IMP-012 document parse task orchestration (D10 matrix).

Exercises idempotency, retry classification, and the status lifecycle without
PostgreSQL or Redis: an in-memory document repository, a fake storage, and a
controllable fake parser stand in for the worker's real resources.
"""
from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import BinaryIO
from uuid import UUID, uuid4

from backend.app.core.errors import AppError
from backend.app.core.settings import Settings
from backend.app.documents.models import DocumentStatus, ResumeDocument
from backend.app.documents.parse_service import (
    CeleryParseEnqueuer,
    DocumentParseService,
    InMemoryDocumentRepository,
    TransientParseError,
    _dict_to_parsed,
)
from backend.app.documents.parsers import (
    DocumentParseError,
    ParsedBlock,
    ParsedDocument,
    PdfLocator,
)
from backend.app.infrastructure.storage import StoredObject

PDF = "application/pdf"


def _settings() -> Settings:
    return SimpleNamespace(  # type: ignore[return-value]
        max_pdf_pages=50,
        max_extracted_chars=200_000,
        parser_timeout_seconds=60,
        parser_version="v1",
        max_transient_retries=2,
    )  # noqa: E501


def _document(*, status: DocumentStatus = DocumentStatus.UPLOADED, attempt: int = 0, parser_version: str | None = None) -> ResumeDocument:  # noqa: E501
    return ResumeDocument(
        id=uuid4(),
        original_filename="resume.pdf",
        storage_key="k1",
        media_type=PDF,
        size_bytes=10,
        content_sha256="deadbeef",
        status=status,
        parser_version=parser_version,
        attempt=attempt,
        uploaded_by=uuid4(),
    )


def _parsed() -> ParsedDocument:
    return ParsedDocument(
        media_type=PDF,
        full_text="hello world",
        blocks=(
            ParsedBlock(
                block_index=0,
                text="hello world",
                locator=PdfLocator(page_number=1, block_index=0, char_start=0, char_end=11),
            ),
        ),
    )


class _FakeParser:
    def __init__(self, *, fail_with: DocumentParseError | None = None, result: ParsedDocument | None = None) -> None:  # noqa: E501
        self.media_types = frozenset({PDF})
        self.version = "v1"
        self.fail_with = fail_with
        self.result = result or _parsed()
        self.calls = 0

    def parse(self, stream: object, limits: object) -> ParsedDocument:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.result


class _FakeStorage:
    """Minimal :class:`StorageBackend` stand-in yielding an in-memory blob."""

    def __init__(self, data: bytes = b"%PDF-1.4 fake") -> None:
        self._data = data
        self.opens = 0

    @asynccontextmanager
    async def open(self, storage_key: str) -> AsyncIterator[BinaryIO]:
        self.opens += 1
        yield io.BytesIO(self._data)

    async def put(self, source: BinaryIO, *, media_type: str) -> StoredObject:
        raise NotImplementedError

    async def delete_if_unreferenced(self, storage_key: str) -> None:
        raise NotImplementedError

    async def healthcheck(self) -> None:
        raise NotImplementedError


class _FakeEnqueuer:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, int, str]] = []

    def enqueue_parse(self, document_id: UUID, *, attempt: int, parser_version: str) -> None:
        self.calls.append((document_id, attempt, parser_version))


def _service(document: ResumeDocument, *, parser: _FakeParser, storage: _FakeStorage | None = None, enqueue: _FakeEnqueuer | None = None) -> DocumentParseService:  # noqa: E501
    repo = InMemoryDocumentRepository()
    repo._store[document.id] = document
    return DocumentParseService(
        documents=repo,
        storage=storage or _FakeStorage(),
        parsers={PDF: parser},
        enqueue=enqueue or _FakeEnqueuer(),
        settings=_settings(),
    )


def test_process_parse_success_persists_review_required() -> None:
    doc = _document()
    parser = _FakeParser()
    service = _service(doc, parser=parser)

    status = asyncio.run(service.process_parse(doc.id, attempt=1, parser_version="v1"))

    assert status is DocumentStatus.REVIEW_REQUIRED
    assert doc.status is DocumentStatus.REVIEW_REQUIRED
    assert doc.attempt == 1
    assert doc.parser_version == "v1"
    assert doc.parsed_json is not None
    assert doc.parsed_json["full_text"] == "hello world"
    assert len(doc.parsed_json["blocks"]) == 1
    assert parser.calls == 1


def test_parsed_json_round_trips_through_dict() -> None:
    doc = _document()
    parser = _FakeParser()
    service = _service(doc, parser=parser)
    asyncio.run(service.process_parse(doc.id, attempt=1, parser_version="v1"))

    restored = _dict_to_parsed(doc.parsed_json)  # type: ignore[arg-type]
    assert restored == _parsed()
    assert restored.blocks[0].locator.page_number == 1  # type: ignore[union-attr]


def test_duplicate_delivery_same_attempt_is_idempotent() -> None:
    doc = _document()
    parser = _FakeParser()
    service = _service(doc, parser=parser)
    assert asyncio.run(service.process_parse(doc.id, attempt=1, parser_version="v1")) is DocumentStatus.REVIEW_REQUIRED  # noqa: E501

    # Re-delivery of the same attempt must not re-parse.
    again = asyncio.run(service.process_parse(doc.id, attempt=1, parser_version="v1"))
    assert again is DocumentStatus.REVIEW_REQUIRED
    assert parser.calls == 1


def test_newer_attempt_wins_on_stale_delivery() -> None:
    doc = _document(status=DocumentStatus.REVIEW_REQUIRED, attempt=2)
    parser = _FakeParser()
    service = _service(doc, parser=parser)

    result = asyncio.run(service.process_parse(doc.id, attempt=1, parser_version="v1"))

    assert result is DocumentStatus.REVIEW_REQUIRED
    assert parser.calls == 0  # stale attempt ignored


def test_transient_parse_error_raises_for_celery_retry() -> None:
    doc = _document()
    parser = _FakeParser(fail_with=DocumentParseError("PARSER_TIMEOUT", "超时", retryable=True))
    service = _service(doc, parser=parser)

    error = None
    try:
        asyncio.run(service.process_parse(doc.id, attempt=1, parser_version="v1"))
    except TransientParseError as exc:
        error = exc
    assert error is not None
    assert error.code == "PARSER_TIMEOUT"
    # Left in PARSING so a retry re-runs from scratch.
    assert doc.status is DocumentStatus.PARSING


def test_terminal_parse_error_marks_failed_and_not_retryable() -> None:
    doc = _document()
    parser = _FakeParser(fail_with=DocumentParseError("INVALID_PDF", "无法解析"))
    service = _service(doc, parser=parser)

    status = asyncio.run(service.process_parse(doc.id, attempt=1, parser_version="v1"))

    assert status is DocumentStatus.FAILED
    assert doc.status is DocumentStatus.FAILED
    assert doc.error_code == "INVALID_PDF"
    assert doc.retryable is False


def test_retry_parse_enqueues_next_attempt_and_queues() -> None:
    doc = _document(status=DocumentStatus.FAILED, attempt=1)
    enqueue = _FakeEnqueuer()
    service = _service(doc, parser=_FakeParser(), enqueue=enqueue)

    status = asyncio.run(service.retry_parse(doc.id))

    assert status is DocumentStatus.QUEUED
    assert doc.status is DocumentStatus.QUEUED
    assert doc.attempt == 2
    assert enqueue.calls == [(doc.id, 2, "v1")]


def test_retry_parse_rejects_non_terminal_state() -> None:
    doc = _document(status=DocumentStatus.REVIEW_REQUIRED, attempt=1)
    service = _service(doc, parser=_FakeParser())

    error = None
    try:
        asyncio.run(service.retry_parse(doc.id))
    except AppError as exc:
        error = exc
    assert error is not None
    assert error.code == "NOT_RETRYABLE"
    assert error.http_status == 409


def test_unexpected_parser_version_is_skipped() -> None:
    doc = _document(parser_version="v0")
    parser = _FakeParser()
    service = _service(doc, parser=parser)

    result = asyncio.run(service.process_parse(doc.id, attempt=1, parser_version="v1"))

    assert result is DocumentStatus.UPLOADED
    assert parser.calls == 0


def test_missing_document_returns_none() -> None:
    parser = _FakeParser()
    service = _service(_document(), parser=parser)
    assert asyncio.run(service.process_parse(uuid4(), attempt=1, parser_version="v1")) is None


def test_celery_enqueuer_builds_named_task_payload() -> None:
    captured: dict[str, object] = {}

    class _StubCelery:
        def send_task(self, name: str, *, args: list[str], kwargs: dict[str, object]) -> None:
            captured["name"] = name
            captured["args"] = args
            captured["kwargs"] = kwargs

    enqueuer = CeleryParseEnqueuer(_StubCelery(), "documents.parse")
    enqueuer.enqueue_parse(uuid4(), attempt=3, parser_version="v2")
    assert captured["name"] == "documents.parse"
    assert captured["kwargs"] == {"attempt": 3, "parser_version": "v2"}
