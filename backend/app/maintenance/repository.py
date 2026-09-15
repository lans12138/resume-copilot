"""Maintenance persistence and storage-scan ports (FIN-006).

The maintenance jobs of :mod:`backend.app.maintenance.service` need surfaces the
request path never uses: a scan for stranded documents, a "is this storage key
still referenced?" check, and an enumerator for objects sitting in the volume.
None of them belong on the existing repositories — ``DocumentRepository`` is the
parse task's narrow boundary and ``StorageBackend`` is write-through by design —
so they are declared here as small focused ports.

Two rules hold across every adapter in this module:

* **Reads see committed state.** Maintenance runs minutes after the fact, so
  every query must observe other processes' commits. Nothing here caches a value
  a worker may have just changed.
* **The in-memory adapters mirror the SQL semantics, not a simplification of
  them.** Ordering, batch caps, and the reference check behave identically in
  both, because a test that passes against memory but would fail against
  PostgreSQL is worse than no test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.checkpoint import SqlCheckpointer
from backend.app.approvals.models import Approval, ApprovalStatus
from backend.app.documents.models import DocumentStatus, ResumeDocument

# The managed layout written by :class:`LocalVolumeStorage`: objects/<2 hex>/<32 hex>.
_OBJECT_SHARD_LENGTH = 2
_OBJECT_NAME_LENGTH = 32


@dataclass(frozen=True, slots=True)
class OrphanFileTarget:
    """One stored object observed by an orphan scan.

    ``storage_key`` is ``None`` when an entry exists on disk but does not match
    the managed layout (a stray file, a leftover staging file). Such an entry
    cannot be addressed through the storage backend, so the scan counts it and
    leaves it alone rather than constructing a path from untrusted input.
    """

    storage_key: str | None
    modified_at: datetime | None


class MaintenanceDocumentRepository(Protocol):
    """Scan and reference surface over ``resume_documents`` for maintenance."""

    async def list_stranded_parses(
        self, *, queued_before: datetime, limit: int
    ) -> list[ResumeDocument]:
        """Documents stuck in ``QUEUED`` since before ``queued_before``.

        ``QUEUED`` is written at upload/retry time and left the moment the parse
        task starts, so a document still there after a grace period is one whose
        publication or consumption never happened. Ordered oldest first and capped
        so one sweep cannot publish an unbounded number of tasks.
        """
        ...

    async def is_storage_key_referenced(self, storage_key: str) -> bool:
        """True if any document row still points at ``storage_key``."""
        ...


class SqlMaintenanceDocumentRepository:
    """PostgreSQL adapter; both queries read committed state only."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_stranded_parses(
        self, *, queued_before: datetime, limit: int
    ) -> list[ResumeDocument]:
        result = await self._session.execute(
            select(ResumeDocument)
            .where(
                ResumeDocument.status == DocumentStatus.QUEUED,
                ResumeDocument.updated_at <= queued_before,
            )
            .order_by(ResumeDocument.updated_at, ResumeDocument.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def is_storage_key_referenced(self, storage_key: str) -> bool:
        result = await self._session.execute(
            select(ResumeDocument.id)
            .where(ResumeDocument.storage_key == storage_key)
            .limit(1)
        )
        return result.first() is not None


class InMemoryMaintenanceDocumentRepository:
    """In-memory mirror of the SQL adapter for hermetic maintenance tests."""

    def __init__(self, documents: dict[UUID, ResumeDocument] | None = None) -> None:
        self._documents = documents if documents is not None else {}

    async def list_stranded_parses(
        self, *, queued_before: datetime, limit: int
    ) -> list[ResumeDocument]:
        stranded = [
            document
            for document in self._documents.values()
            if document.status is DocumentStatus.QUEUED
            and document.updated_at is not None
            and document.updated_at <= queued_before
        ]
        stranded.sort(key=lambda document: (document.updated_at, str(document.id)))
        return stranded[:limit]

    async def is_storage_key_referenced(self, storage_key: str) -> bool:
        return any(document.storage_key == storage_key for document in self._documents.values())


class StoredObjectLister(Protocol):
    """Enumerates and deletes objects in the configured storage backend."""

    async def list_objects(self, *, limit: int) -> list[OrphanFileTarget]: ...

    async def delete_object(self, storage_key: str) -> None: ...


class LocalVolumeStorageLister:
    """Walks the ``objects/<aa>/<32 hex>`` layout written by ``LocalVolumeStorage``.

    Only files matching that layout are reported with a key. Anything else — the
    ``.tmp`` staging directory the backend writes during upload, a stray file —
    is reported with ``storage_key=None`` so the sweep counts it without ever
    turning an unrecognised name into a delete path.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    async def list_objects(self, *, limit: int) -> list[OrphanFileTarget]:
        return await to_thread.run_sync(self._list_sync, limit)

    def _list_sync(self, limit: int) -> list[OrphanFileTarget]:
        objects_root = self._root / "objects"
        if not objects_root.is_dir():
            return []
        found: list[OrphanFileTarget] = []
        # Sorted iteration keeps the scan deterministic; the cap is a hard stop so
        # one cycle cannot walk an unbounded directory tree.
        for shard in sorted(path for path in objects_root.iterdir() if path.is_dir()):
            for entry in sorted(path for path in shard.iterdir() if path.is_file()):
                modified = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)
                found.append(
                    OrphanFileTarget(
                        storage_key=_managed_key(shard.name, entry.name),
                        modified_at=modified,
                    )
                )
                if len(found) >= limit:
                    return found
        return found

    async def delete_object(self, storage_key: str) -> None:
        await to_thread.run_sync(self._delete_sync, storage_key)

    def _delete_sync(self, storage_key: str) -> None:
        path = (self._root / storage_key).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise ValueError("storage key escapes its configured root")
        path.unlink(missing_ok=True)


def _managed_key(shard_name: str, entry_name: str) -> str | None:
    """Return the storage key for a managed-layout entry, else ``None``."""
    if len(shard_name) != _OBJECT_SHARD_LENGTH or len(entry_name) != _OBJECT_NAME_LENGTH:
        return None
    try:
        int(entry_name, 16)
        int(shard_name, 16)
    except ValueError:
        return None
    return f"objects/{shard_name}/{entry_name}"


class SqlApplicationRunRecoveryProbe:
    """PostgreSQL probe for the durable facts that authorise a resume (§14.4).

    Deliberately reads *both* facts from the database on every call, in the same
    transaction the sweep runs in, so the republish scan's decision is based on
    committed state rather than on anything it remembered from a previous sweep.
    The claim in ``agent.tasks`` re-validates them again before executing; this
    probe only avoids publishing deliveries that are provably useless.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_checkpoint_version(self, thread_id: str) -> str | None:
        checkpoint = await SqlCheckpointer(self._session).get(thread_id, "", "")
        return None if checkpoint is None else checkpoint.checkpoint_id

    async def executed_approval_for_run(self, run_id: UUID) -> Approval | None:
        result = await self._session.execute(
            select(Approval)
            .where(
                Approval.application_run_id == run_id,
                Approval.status == ApprovalStatus.EXECUTED,
            )
            .order_by(Approval.executed_at.desc(), Approval.created_at.desc())
        )
        return result.scalars().first()


__all__ = [
    "InMemoryMaintenanceDocumentRepository",
    "LocalVolumeStorageLister",
    "MaintenanceDocumentRepository",
    "OrphanFileTarget",
    "SqlApplicationRunRecoveryProbe",
    "SqlMaintenanceDocumentRepository",
    "StoredObjectLister",
]
