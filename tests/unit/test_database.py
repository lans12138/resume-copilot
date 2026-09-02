"""Explicit SQLAlchemy Unit of Work transaction-boundary tests."""

import asyncio
from typing import cast
from unittest.mock import AsyncMock, Mock

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.infrastructure.database import SessionFactory, SqlAlchemyUnitOfWork


def test_unit_of_work_commits_only_when_explicitly_requested() -> None:
    session = AsyncMock(spec=AsyncSession)
    session_factory = cast(SessionFactory, Mock(return_value=session))

    async def scenario() -> None:
        async with SqlAlchemyUnitOfWork(session_factory) as unit_of_work:
            await unit_of_work.commit()

    asyncio.run(scenario())

    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    session.close.assert_awaited_once()


def test_unit_of_work_rolls_back_an_uncommitted_scope() -> None:
    session = AsyncMock(spec=AsyncSession)
    session_factory = cast(SessionFactory, Mock(return_value=session))

    async def scenario() -> None:
        async with SqlAlchemyUnitOfWork(session_factory):
            pass

    asyncio.run(scenario())

    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()
    session.close.assert_awaited_once()
