"""FIN-001: PostgreSQL integration tests for HTTP request-level idempotency.

These run against a real PostgreSQL (asyncpg) when ``DATABASE_URL`` points at one.
They are skipped otherwise so the normal unit suite stays hermetic. Run them inside
the backend development image attached to the compose network, e.g.::

    docker run --rm --network resume-copilot_backend --env-file .env.example \
        resume-copilot-backend-development:local \
        pytest -q tests/integration/test_idempotency_postgres.py

Scenarios: replay, conflict, concurrent double-click (single side effect), and
stale-lock takeover (API restart) — exercised through the real SQL backend.

Each ``def test_`` runs the async scenario through ``asyncio.run`` (the project's
hermetic unit-test convention: no pytest-asyncio plugin is configured).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.idempotency.models import IdempotencyRecord, IdempotencyStatus
from backend.app.idempotency.repository import SqlIdempotencyRepository
from backend.app.idempotency.service import IdempotencyService

_DATABASE_URL = os.environ.get("DATABASE_URL", "")
_RUN_INTEGRATION = _DATABASE_URL.startswith("postgresql+asyncpg")
pytestmark = pytest.mark.skipif(  # type: ignore[name-defined]
    not _RUN_INTEGRATION, reason="requires DATABASE_URL=postgresql+asyncpg://..."
)


def _engine_and_factory():
    engine = create_async_engine(_DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _ensure_table(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: IdempotencyRecord.__table__.create(sync, checkfirst=True)
        )


async def _clear(engine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM idempotency_records"))


def test_sql_replay_and_conflict() -> None:
    async def _run() -> None:
        engine, factory = _engine_and_factory()
        try:
            await _ensure_table(engine)
            await _clear(engine)
            service = IdempotencyService(SqlIdempotencyRepository(factory))

            out1 = await service.resolve(key="k1", operation="op", actor_id=None, request_hash="h1")
            assert out1.kind == "new"
            await service.record_success(key="k1", status_code=200, body={"v": 1}, resource_id="r1")

            # Same key + same request -> replay (no second side effect needed here).
            out2 = await service.resolve(key="k1", operation="op", actor_id=None, request_hash="h1")
            assert out2.kind == "replay" and out2.body == {"v": 1}

            # Same key + different request -> conflict.
            out3 = await service.resolve(key="k1", operation="op", actor_id=None, request_hash="h2")
            assert out3.kind == "conflict"
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_sql_concurrent_double_click_single_side_effect() -> None:
    async def _run() -> None:
        engine, factory = _engine_and_factory()
        try:
            await _ensure_table(engine)
            await _clear(engine)
            service = IdempotencyService(
                SqlIdempotencyRepository(factory), poll_interval=0.02, max_wait_seconds=5
            )
            side_effects: list[str] = []
            results: dict[str, str] = {}

            async def handle(name: str) -> None:
                outcome = await service.resolve(
                    key="k1", operation="op", actor_id=None, request_hash="h1"
                )
                if outcome.kind == "new":
                    side_effects.append(name)
                    await service.record_success(
                        key="k1", status_code=200, body={"ok": True}, resource_id=None
                    )
                    results[name] = "new"
                else:
                    results[name] = outcome.kind

            await asyncio.gather(handle("A"), handle("B"))
            assert len(side_effects) == 1
            assert set(results.values()) == {"new", "replay"}
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_sql_stale_lock_takeover_after_restart() -> None:
    async def _run() -> None:
        from datetime import datetime

        engine, factory = _engine_and_factory()
        try:
            await _ensure_table(engine)
            await _clear(engine)
            service = IdempotencyService(SqlIdempotencyRepository(factory), ttl_seconds=300)

            # Simulate a crashed-in-progress lock with an old locked_at (beyond TTL).
            async with factory() as session:
                session.add(
                    IdempotencyRecord(
                        key="k1",
                        operation="op",
                        request_hash="h1",
                        status=IdempotencyStatus.IN_PROGRESS,
                        locked_at=datetime(2000, 1, 1, tzinfo=UTC),
                    )
                )
                await session.commit()

            # A retried request takes over the stale lock and re-runs.
            out = await service.resolve(key="k1", operation="op", actor_id=None, request_hash="h1")
            assert out.kind == "new"
            await service.record_success(
                key="k1", status_code=200, body={"ok": True}, resource_id=None
            )

            replay = await service.resolve(
                key="k1", operation="op", actor_id=None, request_hash="h1"
            )
            assert replay.kind == "replay"
        finally:
            await engine.dispose()

    asyncio.run(_run())
