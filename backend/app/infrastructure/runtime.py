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


@dataclass(slots=True)
class RuntimeResources:
    """Owned database, Redis, and Storage resources for one process."""

    engine: AsyncEngine
    session_factory: SessionFactory
    checkpointer: Checkpointer
    redis: Redis
    storage_root: Path
    storage: StorageBackend

    @classmethod
    def build(cls, settings: Settings) -> RuntimeResources:
        assert settings.storage_root is not None
        engine = build_engine(settings)
        storage = LocalVolumeStorage(
            settings.storage_root,
            max_size_bytes=settings.max_file_size_mb * 1024 * 1024,
        )
        # MVP checkpointer: process-local. IMP-030 swaps in an AsyncPostgresSaver
        # so a worker restart can still resume a WAITING_APPROVAL run (§17.2).
        return cls(
            engine=engine,
            session_factory=build_session_factory(engine),
            checkpointer=InMemoryCheckpointer(),
            redis=build_redis_client(settings),
            storage_root=settings.storage_root,
            storage=storage,
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
