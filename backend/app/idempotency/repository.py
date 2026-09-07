"""Idempotency storage ports and backends (FIN-001).

The repository abstracts the lock-and-replay bookkeeping so the service logic can
be exercised without Postgres (in-memory backend for unit tests) and run against
a real database (SQL backend for production + integration tests).

All control operations open their own short transaction and commit immediately so
the ``IN_PROGRESS`` lock is visible to concurrent requests and to a restarted
process. Completion/failure bookkeeping is likewise a separate committed
transaction; the business transaction it guards stays independent (§20.3 FIN-001).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.idempotency.models import IdempotencyRecord, IdempotencyStatus


class IdempotencyRepository(Protocol):
    """Persistence contract for idempotency records."""

    async def claim_new(
        self,
        *,
        key: str,
        operation: str,
        actor_id: UUID | None,
        request_hash: str,
        ttl_seconds: int,
    ) -> bool:
        """Try to own the key as IN_PROGRESS. Return True if we now own it."""

    async def get_by_key(self, key: str) -> IdempotencyRecord | None:
        """Read the current record for a key (or None)."""

    async def takeover_stale(
        self, *, key: str, request_hash: str, ttl_seconds: int
    ) -> bool:
        """Re-claim an IN_PROGRESS/FAILED key whose lock expired. True if taken."""

    async def record_success(
        self, *, key: str, status_code: int, body: dict[str, Any], resource_id: str | None
    ) -> None:
        """Freeze the response as COMPLETED."""

    async def clear_in_progress(self, key: str) -> None:
        """Drop an IN_PROGRESS lock so a retried request can re-run safely."""


class InMemoryIdempotencyRepository:
    """Process-local backend for hermetic unit tests."""

    def __init__(
        self, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._store: dict[str, IdempotencyRecord] = {}
        self._lock = asyncio.Lock()
        self._clock = clock or (lambda: datetime.now(tz=datetime.now().astimezone().tzinfo))

    async def claim_new(
        self,
        *,
        key: str,
        operation: str,
        actor_id: UUID | None,
        request_hash: str,
        ttl_seconds: int,
    ) -> bool:
        async with self._lock:
            if key in self._store:
                return False
            self._store[key] = IdempotencyRecord(
                key=key,
                operation=operation,
                actor_id=actor_id,
                request_hash=request_hash,
                status=IdempotencyStatus.IN_PROGRESS,
                locked_at=self._clock(),
            )
            return True

    async def get_by_key(self, key: str) -> IdempotencyRecord | None:
        async with self._lock:
            return self._store.get(key)

    async def takeover_stale(
        self, *, key: str, request_hash: str, ttl_seconds: int
    ) -> bool:
        async with self._lock:
            record = self._store.get(key)
            if record is None:
                return False
            if record.status not in (IdempotencyStatus.IN_PROGRESS, IdempotencyStatus.FAILED):
                return False
            if record.locked_at is None:
                return False
            if (self._clock() - record.locked_at).total_seconds() <= ttl_seconds:
                return False
            record.status = IdempotencyStatus.IN_PROGRESS
            record.request_hash = request_hash
            record.locked_at = self._clock()
            return True

    async def record_success(
        self, *, key: str, status_code: int, body: dict[str, Any], resource_id: str | None
    ) -> None:
        async with self._lock:
            record = self._store.get(key)
            if record is None:
                return
            record.status = IdempotencyStatus.COMPLETED
            record.response_status = status_code
            record.response_body = body
            record.resource_id = resource_id
            record.locked_at = self._clock()

    async def clear_in_progress(self, key: str) -> None:
        async with self._lock:
            record = self._store.get(key)
            if record is not None and record.status == IdempotencyStatus.IN_PROGRESS:
                del self._store[key]


class SqlIdempotencyRepository:
    """PostgreSQL backend; opens a short control transaction per operation."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim_new(
        self,
        *,
        key: str,
        operation: str,
        actor_id: UUID | None,
        request_hash: str,
        ttl_seconds: int,
    ) -> bool:
        async with self._session_factory() as session:
            stmt = (
                pg_insert(IdempotencyRecord)
                .values(
                    key=key,
                    operation=operation,
                    actor_id=actor_id,
                    request_hash=request_hash,
                    status=IdempotencyStatus.IN_PROGRESS,
                    locked_at=func.now(),
                    expires_at=func.now() + text(f"interval '{int(ttl_seconds)} seconds'"),
                )
                .on_conflict_do_nothing(index_elements=["key"])
                .returning(IdempotencyRecord.id)
            )
            result = await session.execute(stmt)
            # RETURNING + fetchone() (synchronous on the executed Result) reports
            # whether *this* transaction inserted the row. On a unique-key conflict
            # the INSERT is a no-op and returns nothing, so the loser returns False
            # and falls back to polling the leader (FIN-001 #4: exactly one side
            # effect under concurrent double-click in Postgres).
            inserted = result.fetchone() is not None
            await session.commit()
            return inserted

    async def get_by_key(self, key: str) -> IdempotencyRecord | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(IdempotencyRecord).where(IdempotencyRecord.key == key)
            )
            return result.scalar_one_or_none()

    async def takeover_stale(
        self, *, key: str, request_hash: str, ttl_seconds: int
    ) -> bool:
        async with self._session_factory() as session:
            stmt = (
                update(IdempotencyRecord)
                .where(
                    IdempotencyRecord.key == key,
                    IdempotencyRecord.status.in_(
                        [IdempotencyStatus.IN_PROGRESS, IdempotencyStatus.FAILED]
                    ),
                    IdempotencyRecord.locked_at
                    < func.now() - text(f"interval '{int(ttl_seconds)} seconds'"),
                )
                .values(
                    status=IdempotencyStatus.IN_PROGRESS,
                    request_hash=request_hash,
                    locked_at=func.now(),
                )
                .returning(IdempotencyRecord.id)
            )
            result = await session.execute(stmt)
            # RETURNING + fetchone() (synchronous on the executed Result) reports whether
            # the stale-lock row was actually re-claimed. Without RETURNING an UPDATE yields
            # zero result rows and fetchone() is always None, which would make every takeover
            # report failure and break the API-restart recovery path (FIN-001 #4). If another
            # process already took it over, no row matches the WHERE and fetchone() returns
            # None, so we report "not taken" and re-read on the next loop iteration.
            taken = result.fetchone() is not None
            await session.commit()
            return taken

    async def record_success(
        self, *, key: str, status_code: int, body: dict[str, Any], resource_id: str | None
    ) -> None:
        async with self._session_factory() as session:
            stmt = (
                update(IdempotencyRecord)
                .where(IdempotencyRecord.key == key)
                .values(
                    status=IdempotencyStatus.COMPLETED,
                    response_status=status_code,
                    response_body=body,
                    resource_id=resource_id,
                    locked_at=func.now(),
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def clear_in_progress(self, key: str) -> None:
        async with self._session_factory() as session:
            stmt = delete(IdempotencyRecord).where(
                IdempotencyRecord.key == key,
                IdempotencyRecord.status == IdempotencyStatus.IN_PROGRESS,
            )
            await session.execute(stmt)
            await session.commit()
