"""User persistence adapter and normalization policy."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.models import User


def normalize_username(username: str) -> str:
    return unicodedata.normalize("NFKC", username).strip().casefold()


class UserRepository(Protocol):
    async def find_by_username(self, normalized_username: str) -> User | None: ...

    async def find_by_id(self, user_id: UUID) -> User | None: ...

    async def add(self, user: User) -> None: ...


class SqlAlchemyUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_username(self, normalized_username: str) -> User | None:
        return cast(
            User | None,
            await self._session.scalar(
                select(User).where(User.username == normalized_username)
            ),
        )

    async def find_by_id(self, user_id: UUID) -> User | None:
        return cast(User | None, await self._session.get(User, user_id))

    async def add(self, user: User) -> None:
        self._session.add(user)


class InMemoryUserRepository:
    """Small test adapter that follows the same normalized lookup behavior."""

    def __init__(self, users: Sequence[User] = ()) -> None:
        self.users = {user.id: user for user in users}

    async def find_by_username(self, normalized_username: str) -> User | None:
        return next(
            (user for user in self.users.values() if user.username == normalized_username),
            None,
        )

    async def find_by_id(self, user_id: UUID) -> User | None:
        return self.users.get(user_id)

    async def add(self, user: User) -> None:
        self.users[user.id] = user
