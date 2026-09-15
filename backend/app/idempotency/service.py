"""Idempotency resolution logic (FIN-001).

``IdempotencyService.resolve`` turns a request into one of three outcomes:

* ``new`` — this is the leader; run the business logic and call
  ``record_success`` (or let ``clear_in_progress`` clean up on failure).
* ``replay`` — an identical request (same key + same hash) already completed;
  return the frozen response without re-executing.
* ``conflict`` — the same key was reused for a *different* request; reject with
  409 ``IDEMPOTENCY_KEY_REUSED``.

Concurrent leaders serialize through the ``IN_PROGRESS`` lock. A follower that
arrives while the leader is in flight polls until the leader reaches a terminal
state; on completion it replays, on staleness (crash/restart) it takes over and
re-runs. The logic is backend-agnostic and covered by in-memory unit tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from backend.app.core.errors import app_error
from backend.app.idempotency.models import IdempotencyStatus
from backend.app.idempotency.repository import IdempotencyRepository


@dataclass
class AcquireOutcome:
    """Result of resolving an idempotency key."""

    kind: str  # "new" | "replay" | "conflict"
    status: int | None = None
    body: dict[str, Any] | None = None


class IdempotencyService:
    """Resolve ``Idempotency-Key`` requests against a repository."""

    def __init__(
        self,
        repository: IdempotencyRepository,
        *,
        ttl_seconds: int = 300,
        poll_interval: float = 0.05,
        max_wait_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repository
        self._ttl = timedelta(seconds=ttl_seconds)
        self._poll_interval = poll_interval
        self._max_wait = max_wait_seconds
        self._clock = clock or (lambda: datetime.now(tz=datetime.now().astimezone().tzinfo))

    async def resolve(
        self,
        *,
        key: str,
        operation: str,
        actor_id: UUID | None,
        request_hash: str,
    ) -> AcquireOutcome:
        if await self._repo.claim_new(
            key=key,
            operation=operation,
            actor_id=actor_id,
            request_hash=request_hash,
            ttl_seconds=int(self._ttl.total_seconds()),
        ):
            return AcquireOutcome("new")

        waited = 0.0
        while True:
            record = await self._repo.get_by_key(key)
            if record is None:
                # Lost the insert race; try again. Give up only if we cannot own it.
                if await self._repo.claim_new(
                    key=key,
                    operation=operation,
                    actor_id=actor_id,
                    request_hash=request_hash,
                    ttl_seconds=int(self._ttl.total_seconds()),
                ):
                    return AcquireOutcome("new")
                continue

            if record.status == IdempotencyStatus.COMPLETED:
                if record.request_hash == request_hash:
                    return AcquireOutcome("replay", record.response_status, record.response_body)
                return AcquireOutcome("conflict")

            # IN_PROGRESS (concurrent leader) or FAILED (retryable error).
            if record.locked_at is not None and (self._clock() - record.locked_at) > self._ttl:
                if await self._repo.takeover_stale(
                    key=key, request_hash=request_hash, ttl_seconds=int(self._ttl.total_seconds())
                ):
                    return AcquireOutcome("new")
                # Someone else took it over; re-read on the next iteration.
                continue

            if waited >= self._max_wait:
                raise app_error(
                    "IDEMPOTENCY_LOCKED",
                    http_status=409,
                    safe_message="请求仍在处理中，请稍后重试",
                    retryable=True,
                )
            await asyncio.sleep(self._poll_interval)
            waited += self._poll_interval

    async def record_success(
        self, *, key: str, status_code: int, body: dict[str, Any], resource_id: str | None
    ) -> None:
        await self._repo.record_success(
            key=key, status_code=status_code, body=body, resource_id=resource_id
        )

    async def clear_in_progress(self, key: str) -> None:
        await self._repo.clear_in_progress(key)
