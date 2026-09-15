"""FIN-001: HTTP request-level idempotency — logic and concurrency (hermetic).

These tests exercise the idempotency service and repository with the in-memory
backend (no Postgres). They prove the four FIN-001 acceptance scenarios:

* same key + same request -> first result replayed;
* same key + different request -> 409 IDEMPOTENCY_KEY_REUSED (conflict);
* concurrent double-click -> the side effect runs exactly once;
* response loss / API restart -> a stale IN_PROGRESS lock is taken over.

The real PostgreSQL storage path is covered by
tests/integration/test_idempotency_postgres.py.

Each ``def test_`` runs the async scenario through ``asyncio.run`` (the project's
hermetic unit-test convention: no pytest-asyncio plugin is configured).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from backend.app.idempotency.errors import IdempotencyKeyReusedError
from backend.app.idempotency.repository import InMemoryIdempotencyRepository
from backend.app.idempotency.service import IdempotencyService


class _FakeClock:
    def __init__(self, t: datetime) -> None:
        self._t = t

    def now(self) -> datetime:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t = self._t + timedelta(seconds=seconds)


def test_same_key_same_request_replays() -> None:
    async def _run() -> None:
        repo = InMemoryIdempotencyRepository()
        service = IdempotencyService(repo)
        side_effects: list[int] = []

        out1 = await service.resolve(key="k1", operation="op", actor_id=None, request_hash="h1")
        assert out1.kind == "new"
        side_effects.append(1)
        await service.record_success(key="k1", status_code=200, body={"v": 1}, resource_id="r1")

        # Response lost on the wire; client retries with the identical request.
        out2 = await service.resolve(key="k1", operation="op", actor_id=None, request_hash="h1")
        assert out2.kind == "replay"
        assert out2.status == 200
        assert out2.body == {"v": 1}
        # The handler never re-executed.
        assert side_effects == [1]

    asyncio.run(_run())


def test_same_key_different_request_conflicts() -> None:
    async def _run() -> None:
        repo = InMemoryIdempotencyRepository()
        service = IdempotencyService(repo)

        out1 = await service.resolve(key="k1", operation="op", actor_id=None, request_hash="h1")
        assert out1.kind == "new"
        await service.record_success(key="k1", status_code=200, body={"v": 1}, resource_id="r1")

        # Same key reused for a different request body -> different hash -> conflict.
        out2 = await service.resolve(key="k1", operation="op", actor_id=None, request_hash="h2")
        assert out2.kind == "conflict"

    asyncio.run(_run())


def test_concurrent_double_click_single_side_effect() -> None:
    async def _run() -> None:
        repo = InMemoryIdempotencyRepository()
        service = IdempotencyService(repo, poll_interval=0.01, max_wait_seconds=2)
        side_effects: list[str] = []
        results: dict[str, str] = {}

        async def handle(name: str) -> None:
            outcome = await service.resolve(
                key="k1", operation="op", actor_id=None, request_hash="h1"
            )
            if outcome.kind == "new":
                # Run the (only) side effect, then freeze the response.
                side_effects.append(name)
                await service.record_success(
                    key="k1", status_code=200, body={"ok": True}, resource_id=None
                )
                results[name] = "new"
            else:
                results[name] = outcome.kind  # "replay" for the follower

        await asyncio.gather(handle("A"), handle("B"))

        # Exactly one side effect; one caller got the replay, the other the fresh result.
        assert len(side_effects) == 1
        assert set(results.values()) == {"new", "replay"}

    asyncio.run(_run())


def test_api_restart_takes_over_stale_in_progress() -> None:
    async def _run() -> None:
        clock = _FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        repo = InMemoryIdempotencyRepository(clock=clock.now)
        # Share the fake clock with the service so staleness is computed against the
        # same timeline as the repository (independent of the real wall clock).
        service = IdempotencyService(repo, ttl_seconds=300, clock=clock.now)
        attempts: list[str] = []

        # First request claims IN_PROGRESS but the process crashes before recording success.
        out1 = await service.resolve(key="k1", operation="op", actor_id=None, request_hash="h1")
        assert out1.kind == "new"
        attempts.append("before-restart")

        # Process restarts; the lock is now stale (beyond TTL).
        clock.advance(400)
        out2 = await service.resolve(key="k1", operation="op", actor_id=None, request_hash="h1")
        assert out2.kind == "new"  # taken over
        attempts.append("after-restart")
        await service.record_success(key="k1", status_code=200, body={"ok": True}, resource_id=None)

        # A later identical request replays the stored response.
        out3 = await service.resolve(key="k1", operation="op", actor_id=None, request_hash="h1")
        assert out3.kind == "replay"
        assert attempts == ["before-restart", "after-restart"]

    asyncio.run(_run())


def test_failed_request_clears_in_progress_lock() -> None:
    async def _run() -> None:
        repo = InMemoryIdempotencyRepository()
        service = IdempotencyService(repo)

        out1 = await service.resolve(key="k1", operation="op", actor_id=None, request_hash="h1")
        assert out1.kind == "new"
        # Handler raised before record_success -> the lock must be cleared so a retry re-runs.
        await service.clear_in_progress(key="k1")
        assert await repo.get_by_key("k1") is None

        out2 = await service.resolve(key="k1", operation="op", actor_id=None, request_hash="h1")
        assert out2.kind == "new"

    asyncio.run(_run())


def test_idempotency_key_reused_error_contract() -> None:
    async def _run() -> None:
        err = IdempotencyKeyReusedError()
        assert err.http_status == 409
        assert err.code == "IDEMPOTENCY_KEY_REUSED"

    asyncio.run(_run())
