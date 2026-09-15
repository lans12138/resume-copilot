"""Storage boundary and untrusted upload validation tests."""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path

import pytest

from backend.app.documents.validation import (
    DOCX_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    UploadRejected,
    safe_filename,
    validate_upload,
)
from backend.app.infrastructure.storage import (
    InvalidStorageKey,
    LocalVolumeStorage,
    StorageLimitExceeded,
)


def make_docx(*, macro: bool = False) -> io.BytesIO:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        content_type = (
            "application/vnd.ms-word.document.macroEnabled.main+xml"
            if macro
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
        )
        archive.writestr(
            "[Content_Types].xml",
            '<Types><Override PartName="/word/document.xml" '
            f'ContentType="{content_type}"/></Types>',
        )
        archive.writestr("word/document.xml", "<w:document>synthetic resume</w:document>")
        if macro:
            archive.writestr("word/vbaProject.bin", b"synthetic macro")
    stream.seek(0)
    return stream


def test_local_volume_storage_streams_hashes_and_uses_generated_key(tmp_path: Path) -> None:
    storage = LocalVolumeStorage(tmp_path, max_size_bytes=1024)
    content = b"%PDF-1.7\nsynthetic"

    async def scenario() -> None:
        stored = await storage.put(io.BytesIO(content), media_type=PDF_MEDIA_TYPE)
        assert stored.storage_key.startswith("objects/")
        assert stored.size_bytes == len(content)
        assert stored.content_sha256 == (
            "5aea7a7a5e33d66d021fd52802ceb64ac5b8f377b2be55fddca8607f093ce3ce"
        )
        async with storage.open(stored.storage_key) as handle:
            assert handle.read() == content

    asyncio.run(scenario())


def test_local_volume_storage_rejects_traversal_and_cleans_oversize_temp(
    tmp_path: Path,
) -> None:
    storage = LocalVolumeStorage(tmp_path, max_size_bytes=8)

    async def scenario() -> None:
        with pytest.raises(StorageLimitExceeded):
            await storage.put(io.BytesIO(b"0123456789"), media_type=PDF_MEDIA_TYPE)
        with pytest.raises(InvalidStorageKey):
            async with storage.open("../../outside"):
                pass

    asyncio.run(scenario())
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_upload_validation_accepts_matching_pdf_and_docx() -> None:
    pdf = io.BytesIO(b"%PDF-1.7\nsynthetic")
    assert validate_upload(
        pdf,
        filename="resume.pdf",
        declared_media_type=PDF_MEDIA_TYPE,
        declared_size=pdf.getbuffer().nbytes,
        max_size_bytes=1024,
    ).media_type == PDF_MEDIA_TYPE

    docx = make_docx()
    assert validate_upload(
        docx,
        filename="resume.docx",
        declared_media_type=DOCX_MEDIA_TYPE,
        declared_size=docx.getbuffer().nbytes,
        max_size_bytes=1024 * 1024,
    ).media_type == DOCX_MEDIA_TYPE


@pytest.mark.parametrize(
    ("filename", "media_type", "payload", "expected_code"),
    [
        ("resume.pdf", PDF_MEDIA_TYPE, b"not-a-pdf", "SIGNATURE_MISMATCH"),
        ("resume.docx", PDF_MEDIA_TYPE, b"%PDF-1.7", "UNSUPPORTED_MEDIA"),
        ("resume.exe", "application/octet-stream", b"MZ", "UNSUPPORTED_MEDIA"),
    ],
)
def test_upload_validation_rejects_spoofed_files(
    filename: str,
    media_type: str,
    payload: bytes,
    expected_code: str,
) -> None:
    with pytest.raises(UploadRejected) as error:
        validate_upload(
            io.BytesIO(payload),
            filename=filename,
            declared_media_type=media_type,
            declared_size=len(payload),
            max_size_bytes=1024,
        )
    assert error.value.code == expected_code


def test_upload_validation_rejects_macro_docx_and_sanitizes_display_name() -> None:
    docx = make_docx(macro=True)
    with pytest.raises(UploadRejected) as error:
        validate_upload(
            docx,
            filename="resume.docx",
            declared_media_type=DOCX_MEDIA_TYPE,
            declared_size=docx.getbuffer().nbytes,
            max_size_bytes=1024 * 1024,
        )
    assert error.value.code == "UNSAFE_DOCX_CONTENT"
    assert safe_filename("../../candidate.pdf") == "candidate.pdf"
    assert safe_filename("..\\..\\candidate.pdf") == "candidate.pdf"
