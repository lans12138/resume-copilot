"""Alembic environment using the same validated Settings contract as the API."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.core.settings import get_settings
from backend.app.infrastructure.model_registry import get_model_metadata

target_metadata = get_model_metadata()

# Diagnostic: surface the resolved target metadata so a "missing table" failure
# in CI (e.g. alembic check reporting a remove_table) can be traced to the exact
# set of registered tables. Cheap and only emitted while alembic runs.
import os as _os
import sys as _sys

if _os.environ.get("ALEMBIC_DIAGNOSE"):
    _tables = sorted(target_metadata.tables)
    _sys.stderr.write(
        f"[alembic-env] table_count={len(_tables)} "
        f"has_idempotency_records={'idempotency_records' in target_metadata.tables}\n"
        f"[alembic-env] tables={_tables}\n"
    )


def database_url() -> str:
    settings = get_settings()
    assert settings.database_url is not None
    return settings.database_url.get_secret_value()


def run_migrations_offline() -> None:
    """Generate SQL without opening a database connection."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def apply_migrations(connection: AsyncConnection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations through a short-lived asyncpg connection pool."""
    engine = create_async_engine(database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(apply_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
