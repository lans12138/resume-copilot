"""Controlled CLI for creating the MVP's pre-provisioned accounts."""

from __future__ import annotations

import asyncio
import os

from backend.app.auth.models import User, UserRole
from backend.app.auth.passwords import password_service
from backend.app.auth.repository import SqlAlchemyUserRepository, normalize_username
from backend.app.core.settings import get_settings
from backend.app.infrastructure.database import build_engine, build_session_factory


def required_environment(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value:
        raise RuntimeError(f"missing required bootstrap environment: {name}")
    return value


async def create_bootstrap_user() -> None:
    username = normalize_username(required_environment("BOOTSTRAP_USERNAME"))
    password = required_environment("BOOTSTRAP_PASSWORD")
    role = UserRole(required_environment("BOOTSTRAP_ROLE"))
    if not username or len(username) > 128:
        raise RuntimeError("bootstrap username must contain 1 to 128 normalized characters")
    if len(password) < 12:
        raise RuntimeError("bootstrap password must contain at least 12 characters")

    engine = build_engine(get_settings())
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as session:
            users = SqlAlchemyUserRepository(session)
            if await users.find_by_username(username) is not None:
                raise RuntimeError("bootstrap username already exists")
            await users.add(
                User(
                    username=username,
                    password_hash=password_service.hash(password),
                    role=role,
                    is_active=True,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()
    print(f"BOOTSTRAP_USER_CREATED username={username} role={role.value}")


def main() -> None:
    asyncio.run(create_bootstrap_user())


if __name__ == "__main__":
    main()
