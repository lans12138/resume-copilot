"""Approval persistence ports and adapters (IMP-022).

The repository enforces the one non-obvious rule of the approval gate (§4.5/§11):
the partial unique index ``uq_approvals_pending_run`` allows at most one
``PENDING`` approval per run, and the global unique index on ``idempotency_key``
dedupes the business key. The in-memory adapter mirrors both with an explicit
key-collision check (raising ``APPROVAL_IDEMPOTENCY_CONFLICT``/409), while the SQL
adapter leans on the database constraint. ``get_pending_by_run`` powers the
"current PENDING Approval" contract that ``ApplicationRunDetail`` returns (§12.5),
and ``get_expired_pending`` feeds the IMP-023 sweeper.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.approvals.models import Approval, ApprovalStatus
from backend.app.core.errors import app_error


class ApprovalRepository(Protocol):
    """Persistence contract for approval decisions."""

    async def save_approval(self, approval: Approval) -> None: ...
    async def get_approval(self, approval_id: UUID) -> Approval | None: ...
    async def get_pending_by_run(self, application_run_id: UUID) -> Approval | None: ...
    async def get_by_idempotency_key(self, key: str) -> Approval | None: ...
    async def get_expired_pending(self, before: datetime) -> list[Approval]: ...
    async def claim_expired_pending(
        self, before: datetime, *, limit: int
    ) -> list[Approval]:
        """Row-lock one *batch* of timed-out approvals for this sweeper (§11.8).

        Returns candidates in a stable order and holds a row lock on each until the
        caller commits. Concurrent sweepers therefore never overlap: the second
        one skips the rows the first has locked, instead of both expiring them.
        Re-delivered work is safe because the per-record transition is guarded by
        the "still PENDING" re-check, not by who fetched the row.
        """
        ...


class InMemoryApprovalRepository:
    """Lock-guarded in-process store; mirrors the two unique indexes."""

    def __init__(self) -> None:
        self._approvals: dict[UUID, Approval] = {}
        self._keys: dict[str, UUID] = {}
        self._lock = asyncio.Lock()

    async def save_approval(self, approval: Approval) -> None:
        async with self._lock:
            existing = self._keys.get(approval.idempotency_key)
            if existing is not None and existing != approval.id:
                raise app_error(
                    "APPROVAL_IDEMPOTENCY_CONFLICT",
                    http_status=409,
                    safe_message="相同幂等键的审批已存在",
                    details={"idempotency_key": approval.idempotency_key},
                )
            self._keys[approval.idempotency_key] = approval.id
            self._approvals[approval.id] = approval

    async def get_approval(self, approval_id: UUID) -> Approval | None:
        async with self._lock:
            return self._approvals.get(approval_id)

    async def get_pending_by_run(self, application_run_id: UUID) -> Approval | None:
        async with self._lock:
            pending = [
                a for a in self._approvals.values() if a.application_run_id == application_run_id
            ]
            pending = [a for a in pending if a.status == ApprovalStatus.PENDING]
            if not pending:
                return None
            # Latest by created_at, then version.
            return max(pending, key=lambda a: (a.created_at, a.version))

    async def get_by_idempotency_key(self, key: str) -> Approval | None:
        async with self._lock:
            approval_id = self._keys.get(key)
            if approval_id is None:
                return None
            return self._approvals.get(approval_id)

    async def get_expired_pending(self, before: datetime) -> list[Approval]:
        async with self._lock:
            out = [
                a
                for a in self._approvals.values()
                if a.status == ApprovalStatus.PENDING
                and a.expires_at is not None
                and a.expires_at <= before
            ]
            return sorted(out, key=lambda a: a.id)

    async def claim_expired_pending(
        self, before: datetime, *, limit: int
    ) -> list[Approval]:
        """Batch form of :meth:`get_expired_pending` (stable order, capped).

        The in-memory adapter has no row locks, so it mimics ``SKIP LOCKED`` by
        simply returning the first ``limit`` candidates: correctness still rests on
        the "still PENDING" re-check performed per record by the sweeper.
        """
        return (await self.get_expired_pending(before))[:limit]


class SqlApprovalRepository:
    """PostgreSQL adapter; uniqueness enforced by the schema constraints."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_approval(self, approval: Approval) -> None:
        self._session.add(approval)
        await self._session.flush()

    async def get_approval(self, approval_id: UUID) -> Approval | None:
        return await self._session.get(Approval, approval_id)

    async def get_pending_by_run(self, application_run_id: UUID) -> Approval | None:
        result = await self._session.execute(
            select(Approval)
            .where(
                Approval.application_run_id == application_run_id,
                Approval.status == ApprovalStatus.PENDING,
            )
            .order_by(Approval.created_at.desc(), Approval.version.desc())
        )
        return result.scalars().first()

    async def get_by_idempotency_key(self, key: str) -> Approval | None:
        result = await self._session.execute(
            select(Approval).where(Approval.idempotency_key == key)
        )
        return result.scalars().first()

    async def get_expired_pending(self, before: datetime) -> list[Approval]:
        result = await self._session.execute(
            select(Approval)
            .where(Approval.status == ApprovalStatus.PENDING, Approval.expires_at <= before)
            .order_by(Approval.id)
        )
        return list(result.scalars().all())

    async def claim_expired_pending(
        self, before: datetime, *, limit: int
    ) -> list[Approval]:
        """``FOR UPDATE SKIP LOCKED`` batch, ordered by ``id`` (§11.8).

        Two things matter here and neither is optional:

        * **Stable order** (``ORDER BY id``) so two sweepers walking the same
          backlog contend on the same rows in the same sequence rather than
          deadlocking on a partially overlapping set.
        * **SKIP LOCKED** so a second worker does not *block* on rows the first one
          already holds — it moves on to the next candidates and the backlog is
          drained in parallel. This is what makes "多 Worker 竞争" safe instead of
          merely slow.

        The lock is held until the caller commits, which bounds how long a batch
        may be in flight. The batch is intentionally small for that reason.
        """
        result = await self._session.execute(
            select(Approval)
            .where(Approval.status == ApprovalStatus.PENDING, Approval.expires_at <= before)
            .order_by(Approval.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(result.scalars().all())
