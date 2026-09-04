"""Schedule backend Protocol and in-memory MockScheduleBackend (IMP-024, §11.7).

The backend is the seam between the (audited, idempotent) execution service and
the outside world. A real calendar integration must later move to an Outbox and
must never hold a lock across the network call (§11.7, line 975); the Mock keeps
everything in a controlled, lock-guarded in-process store so tests can prove the
"stable external id / execute exactly once" contract without a database.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from backend.app.interviews.schemas import (
    ScheduleProposal,
    ScheduleResult,
    ScheduleStatus,
)


class ScheduleBackend(Protocol):
    """Contract a schedule backend must satisfy (§11.7, line 970-973)."""

    async def create(
        self, *, proposal: ScheduleProposal, idempotency_key: str, application_id: UUID
    ) -> ScheduleResult: ...

    async def get_by_idempotency_key(self, key: str) -> ScheduleResult | None: ...


def _stable_external_id(key: str) -> str:
    """Deterministic external id derived from the idempotency key (§11.7)."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"mock-sched-{digest}"


class MockScheduleBackend:
    """In-memory backend; idempotent on ``idempotency_key`` (§11.7)."""

    def __init__(self) -> None:
        self._store: dict[str, ScheduleResult] = {}
        self._lock = asyncio.Lock()
        # Call counters let tests assert exactly-once execution.
        self.create_calls = 0

    async def get_by_idempotency_key(self, key: str) -> ScheduleResult | None:
        async with self._lock:
            return self._store.get(key)

    async def create(
        self, *, proposal: ScheduleProposal, idempotency_key: str, application_id: UUID
    ) -> ScheduleResult:
        async with self._lock:
            existing = self._store.get(idempotency_key)
            if existing is not None:
                # Idempotent: a duplicate request returns the original result.
                return existing
            self.create_calls += 1
            result = ScheduleResult(
                external_schedule_id=_stable_external_id(idempotency_key),
                status=ScheduleStatus.SCHEDULED,
                application_id=application_id,
                created_at=datetime.now(tz=UTC),
                proposal=proposal,
            )
            self._store[idempotency_key] = result
            return result
