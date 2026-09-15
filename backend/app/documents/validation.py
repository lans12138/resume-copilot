"""Untrusted resume upload metadata and signature validation."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    original_filename: str
    media_type: str


class UploadRejected(Exception):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


def safe_filename(filename: str | None) -> str:
    normalized = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not normalized or len(normalized) > 255 or any(ord(char) < 32 for char in normalized):
        raise UploadRejected("INVALID_FILENAME", "文件名无效")
    return normalized


def validate_upload(
    source: BinaryIO,
    *,
    filename: str | None,
    declared_media_type: str | None,
    declared_size: int | None,
    max_size_bytes: int,
) -> ValidatedUpload:
    original_filename = safe_filename(filename)
    extension = PurePosixPath(original_filename).suffix.casefold()
    media_type = (declared_media_type or "").partition(";")[0].strip().casefold()
    expected = {".pdf": PDF_MEDIA_TYPE, ".docx": DOCX_MEDIA_TYPE}.get(extension)
    if expected is None or media_type != expected:
        raise UploadRejected("UNSUPPORTED_MEDIA", "仅支持扩展名、MIME 一致的 PDF 或 DOCX")
    if declared_size is not None and declared_size > max_size_bytes:
        raise UploadRejected("FILE_TOO_LARGE", "文件超过允许的大小")
    if declared_size == 0:
        raise UploadRejected("EMPTY_FILE", "不接受空文件")

    try:
        source.seek(0)
        if media_type == PDF_MEDIA_TYPE:
            if source.read(5) != b"%PDF-":
                raise UploadRejected("SIGNATURE_MISMATCH", "PDF 文件签名与声明不一致")
        else:
            _validate_docx(source, max_size_bytes=max_size_bytes)
    finally:
        source.seek(0)
    return ValidatedUpload(original_filename=original_filename, media_type=media_type)


def _validate_docx(source: BinaryIO, *, max_size_bytes: int) -> None:
    if not zipfile.is_zipfile(source):
        raise UploadRejected("SIGNATURE_MISMATCH", "DOCX 文件签名与声明不一致")
    source.seek(0)
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            names = {item.filename for item in infos}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise UploadRejected("INVALID_DOCX", "DOCX 缺少必要的 OOXML 内容")
            if any(item.flag_bits & 0x1 for item in infos):
                raise UploadRejected("ENCRYPTED_ARCHIVE", "不接受加密 DOCX")
            if any(_unsafe_member(item.filename) for item in infos):
                raise UploadRejected("UNSAFE_ARCHIVE_PATH", "DOCX 包含不安全路径")
            if any(
                item.filename.casefold().endswith("vbaproject.bin")
                or item.filename.casefold().startswith(("word/activex/", "word/embeddings/"))
                for item in infos
            ):
                raise UploadRejected("UNSAFE_DOCX_CONTENT", "不接受宏或嵌入对象")
            total_uncompressed = sum(item.file_size for item in infos)
            total_compressed = max(1, sum(item.compress_size for item in infos))
            if (
                total_uncompressed > max_size_bytes * 10
                or total_uncompressed / total_compressed > 100
            ):
                raise UploadRejected("ARCHIVE_LIMIT_EXCEEDED", "DOCX 解压规模超出安全限制")
            content_types = archive.read("[Content_Types].xml")
            if b"macroEnabled" in content_types or len(content_types) > 1024 * 1024:
                raise UploadRejected("UNSAFE_DOCX_CONTENT", "不接受启用宏的 DOCX")
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise UploadRejected("INVALID_DOCX", "DOCX 压缩包无效") from error


def _unsafe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return path.is_absolute() or ".." in path.parts
