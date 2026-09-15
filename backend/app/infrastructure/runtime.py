"""Process-local infrastructure resources shared by health and application services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.app.agent.checkpoint import Checkpointer, InMemoryCheckpointer
from backend.app.core.settings import Settings
from backend.app.infrastructure.database import (
    SessionFactory,
    build_engine,
    build_session_factory,
    check_database,
)
from backend.app.infrastructure.redis import build_redis_client, check_redis
from backend.app.infrastructure.storage import LocalVolumeStorage, StorageBackend
from backend.app.sse.notifier import EventNotifier, RedisEventNotifier


@dataclass(slots=True)
class RuntimeResources:
    """Owned database, Redis, and Storage resources for one process."""

    engine: AsyncEngine
    session_factory: SessionFactory
    checkpointer: Checkpointer
    redis: Redis
    storage_root: Path
    storage: StorageBackend
    event_notifier: EventNotifier

    @classmethod
    def build(cls, settings: Settings) -> RuntimeResources:
        assert settings.storage_root is not None
        engine = build_engine(settings)
        storage = LocalVolumeStorage(
            settings.storage_root,
            max_size_bytes=settings.max_file_size_mb * 1024 * 1024,
        )
        redis = build_redis_client(settings)
        # Process-local fallback for services that never resume graphs. The
        # ApplicationRun transaction wires a session-scoped SqlCheckpointer so a
        # WAITING_APPROVAL run survives API/worker restarts (§17.2).
        # SSE fan-out uses Redis Pub/Sub; a lost publish is recovered by the SSE
        # heartbeat polling PostgreSQL (§13.2).
        return cls(
            engine=engine,
            session_factory=build_session_factory(engine),
            checkpointer=InMemoryCheckpointer(),
            redis=redis,
            storage_root=settings.storage_root,
            storage=storage,
            event_notifier=RedisEventNotifier(redis),
        )

    async def check_postgres(self) -> None:
        await check_database(self.engine)

    async def check_redis(self) -> None:
        await check_redis(self.redis)

    async def check_storage(self) -> None:
        await self.storage.healthcheck()

    async def close(self) -> None:
        await self.redis.aclose()
        await self.engine.dispose()
