"""``GET /documents/{id}/content`` backing service (FIN-004, hermetic).

The review screen shows the parsed原文 next to each evidence locator, which needs
the parser's block-and-locator payload. These tests pin the read contract without
a database: the service pulls the stored payload through ``parsed_from_json`` and
re-emits it, so a drifted row fails loudly instead of shipping coordinates that
mis-highlight evidence.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError
from backend.app.documents.models import DocumentStatus, ResumeDocument
from backend.app.documents.parse_service import parsed_to_json
from backend.app.documents.parsers import (
    DocxParagraphLocator,
    ParsedBlock,
    ParsedDocument,
    PdfLocator,
)
from backend.app.documents.service import DocumentUploadService

_DOC_ID = UUID("00000000-0000-0000-0000-0000000000d1")


class _FakeSession:
    """Minimal ``session.get`` stand-in; the service only reads one row."""

    def __init__(self, document: ResumeDocument | None) -> None:
        self.document = document

    async def get(self, model: type[object], key: object) -> object | None:
        assert model is ResumeDocument, "the content read must not touch another table"
        if self.document is None or key != self.document.id:
            return None
        return self.document


def _hr() -> Actor:
    return Actor(user_id=uuid4(), username="hr.reviewer", role=UserRole.HR)


def _manager() -> Actor:
    return Actor(user_id=uuid4(), username="hm.reviewer", role=UserRole.HIRING_MANAGER)


def _document(*, parsed: dict[str, Any] | None, status: DocumentStatus) -> ResumeDocument:
    return ResumeDocument(
        id=_DOC_ID,
        original_filename="resume.docx",
        storage_key="objects/resume.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=2048,
        content_sha256="a" * 64,
        status=status,
        attempt=1,
        retryable=False,
        parsed_json=parsed,
        uploaded_by=uuid4(),
    )


def _service(document: ResumeDocument | None) -> DocumentUploadService:
    return DocumentUploadService(
        cast(AsyncSession, _FakeSession(document)),
        storage=cast(Any, None),
        max_file_size_bytes=1024,
        parser_version="parser-v1",
    )


def _parsed_payload() -> dict[str, Any]:
    parsed = ParsedDocument(
        media_type="application/pdf",
        full_text="张伟\n6 年 Python 后端开发经验",
        blocks=(
            ParsedBlock(block_index=0, text="张伟", locator=PdfLocator(1, 0, 0, 2)),
            ParsedBlock(
                block_index=1,
                text="6 年 Python 后端开发经验",
                locator=DocxParagraphLocator(paragraph_index=2, char_start=0, char_end=13),
            ),
        ),
        page_count=1,
        paragraph_count=2,
        warnings=("SYNTHETIC_WARNING",),
    )
    return parsed_to_json(parsed)


def test_content_returns_blocks_with_source_locators() -> None:
    payload = _parsed_payload()
    service = _service(_document(parsed=payload, status=DocumentStatus.REVIEW_REQUIRED))

    result = asyncio.run(service.get_content(_hr(), _DOC_ID))

    assert result.document_id == _DOC_ID
    assert result.full_text == "张伟\n6 年 Python 后端开发经验"
    assert result.page_count == 1
    assert result.warnings == ["SYNTHETIC_WARNING"]
    assert [block.text for block in result.blocks] == ["张伟", "6 年 Python 后端开发经验"]
    # The locator must survive the round trip verbatim: the UI highlights the
    # excerpt by these coordinates, so drift here silently mis-highlights.
    assert result.blocks[0].locator == {
        "kind": "pdf",
        "page_number": 1,
        "block_index": 0,
        "char_start": 0,
        "char_end": 2,
    }
    assert result.blocks[1].locator == {
        "kind": "docx_paragraph",
        "paragraph_index": 2,
        "char_start": 0,
        "char_end": 13,
    }


def test_content_is_hr_only() -> None:
    service = _service(_document(parsed=_parsed_payload(), status=DocumentStatus.READY))

    with pytest.raises(AppError) as error:
        asyncio.run(service.get_content(_manager(), _DOC_ID))

    assert error.value.code == "FORBIDDEN"
    assert error.value.http_status == 403


def test_content_rejects_unparsed_document() -> None:
    service = _service(_document(parsed=None, status=DocumentStatus.QUEUED))

    with pytest.raises(AppError) as error:
        asyncio.run(service.get_content(_hr(), _DOC_ID))

    # A queued document has no正文 to review yet; this is a state conflict, not a
    # 404, because the document itself exists.
    assert error.value.code == "DOCUMENT_NOT_PARSED"
    assert error.value.http_status == 409


def test_content_reports_missing_document() -> None:
    service = _service(None)

    with pytest.raises(AppError) as error:
        asyncio.run(service.get_content(_hr(), _DOC_ID))

    assert error.value.code == "DOCUMENT_NOT_FOUND"
    assert error.value.http_status == 404


def test_content_rejects_drifted_payload_instead_of_shipping_garbage() -> None:
    service = _service(_document(parsed={"full_text": "x"}, status=DocumentStatus.REVIEW_REQUIRED))

    with pytest.raises(AppError) as error:
        asyncio.run(service.get_content(_hr(), _DOC_ID))

    assert error.value.code == "DOCUMENT_CONTENT_UNAVAILABLE"
    assert error.value.http_status == 422
