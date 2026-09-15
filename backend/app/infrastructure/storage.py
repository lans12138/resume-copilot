"""Safe local-volume object storage with bounded, atomic writes."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import uuid4

from anyio import to_thread

_STORAGE_KEY = re.compile(r"objects/[0-9a-f]{2}/[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class StoredObject:
    storage_key: str
    media_type: str
    size_bytes: int
    content_sha256: str


class StorageLimitExceeded(Exception):
    """Raised after a stream exceeds the configured byte limit."""


class InvalidStorageKey(ValueError):
    """Raised before an untrusted key can become a filesystem path."""


class StorageBackend(Protocol):
    async def put(self, source: BinaryIO, *, media_type: str) -> StoredObject: ...

    def open(self, storage_key: str) -> AbstractAsyncContextManager[BinaryIO]: ...

    async def delete_if_unreferenced(self, storage_key: str) -> None: ...

    async def healthcheck(self) -> None: ...


class LocalVolumeStorage:
    """Store extensionless objects below a fixed root using generated keys only."""

    def __init__(self, root: Path, *, max_size_bytes: int) -> None:
        if not root.is_absolute():
            raise ValueError("storage root must be absolute")
        if max_size_bytes <= 0:
            raise ValueError("max_size_bytes must be positive")
        self.root = root
        self.max_size_bytes = max_size_bytes

    async def put(self, source: BinaryIO, *, media_type: str) -> StoredObject:
        return await to_thread.run_sync(self._put_sync, source, media_type)

    def _put_sync(self, source: BinaryIO, media_type: str) -> StoredObject:
        temporary_root = self.root / ".tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size_bytes = 0
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=temporary_root, delete=False) as target:
                temporary_path = Path(target.name)
                while chunk := source.read(64 * 1024):
                    size_bytes += len(chunk)
                    if size_bytes > self.max_size_bytes:
                        raise StorageLimitExceeded
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if size_bytes == 0:
                raise ValueError("empty files are not accepted")

            object_id = uuid4().hex
            storage_key = f"objects/{object_id[:2]}/{object_id}"
            final_path = self._resolve_key(storage_key)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_path, final_path)
            temporary_path = None
            return StoredObject(
                storage_key=storage_key,
                media_type=media_type,
                size_bytes=size_bytes,
                content_sha256=digest.hexdigest(),
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @asynccontextmanager
    async def open(self, storage_key: str) -> AsyncIterator[BinaryIO]:
        path = self._resolve_key(storage_key)
        handle = await to_thread.run_sync(lambda: path.open("rb"))
        try:
            yield handle
        finally:
            await to_thread.run_sync(handle.close)

    async def delete_if_unreferenced(self, storage_key: str) -> None:
        path = self._resolve_key(storage_key)
        await to_thread.run_sync(path.unlink, True)

    async def healthcheck(self) -> None:
        await to_thread.run_sync(check_storage, self.root)

    def _resolve_key(self, storage_key: str) -> Path:
        if _STORAGE_KEY.fullmatch(storage_key) is None:
            raise InvalidStorageKey("storage key has an invalid format")
        path = (self.root / storage_key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise InvalidStorageKey("storage key escapes its configured root")
        return path


def check_storage(storage_root: Path) -> None:
    """Verify bounded create/read/delete access inside the mounted root."""
    if not storage_root.is_dir():
        raise RuntimeError("storage root is not an existing directory")

    probe_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b", prefix=".readiness-", dir=storage_root, delete=False
        ) as probe:
            probe.write(b"ready")
            probe.flush()
            os.fsync(probe.fileno())
            probe_path = Path(probe.name)
        if probe_path.read_bytes() != b"ready":
            raise RuntimeError("storage readiness content mismatch")
    finally:
        if probe_path is not None:
            probe_path.unlink(missing_ok=True)
