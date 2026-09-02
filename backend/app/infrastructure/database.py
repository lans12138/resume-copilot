"""SQLAlchemy async engine, session factory, and transaction boundary."""

from __future__ import annotations

from types import TracebackType

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from backend.app.core.settings import Settings


class Base(DeclarativeBase):
    """Shared declarative metadata root for all domain models."""


SessionFactory = async_sessionmaker[AsyncSession]


def build_engine(settings: Settings) -> AsyncEngine:
    """Build the process-local async engine without opening a connection."""
    assert settings.database_url is not None
    return create_async_engine(
        settings.database_url.get_secret_value(),
        echo=settings.db_echo,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        pool_timeout=settings.db_pool_timeout,
    )


def build_session_factory(engine: AsyncEngine) -> SessionFactory:
    """Create sessions that keep ORM state usable after explicit commits."""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def check_database(engine: AsyncEngine) -> None:
    """Prove that PostgreSQL accepts a trivial read-only query."""
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))
        if result.scalar_one() != 1:
            raise RuntimeError("database health query returned an unexpected value")


class SqlAlchemyUnitOfWork:
    """Explicit transaction boundary; repositories never commit implicitly."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._completed = False

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("unit of work has not been entered")
        return self._session

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        self._session = self._session_factory()
        self._completed = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        if self._session is None:
            return
        try:
            if not self._completed:
                await self._session.rollback()
        finally:
            await self._session.close()
            self._session = None

    async def commit(self) -> None:
        await self.session.commit()
        self._completed = True

    async def rollback(self) -> None:
        await self.session.rollback()
        self._completed = True
