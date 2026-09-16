"""Controlled CLI for creating the MVP's pre-provisioned accounts.

The bootstrap step is part of the documented one-command start
(``scripts/start_stack.ps1``), which runs it *after* seeding. The seed
script already provisions the demo accounts, so re-running the stack must
not fail on an existing username: an identical, active account is an
idempotent no-op (``BOOTSTRAP_USER_EXISTS``), and only a genuine credential
conflict (different password, role, or a disabled user) is an error.
"""

from __future__ import annotations

import asyncio
import os

from backend.app.auth.models import User, UserRole
from backend.app.auth.passwords import password_service
from backend.app.auth.repository import (
    SqlAlchemyUserRepository,
    UserRepository,
    normalize_username,
)
from backend.app.core.settings import get_settings
from backend.app.infrastructure.database import build_engine, build_session_factory


def required_environment(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value:
        raise RuntimeError(f"missing required bootstrap environment: {name}")
    return value


async def ensure_bootstrap_user(
    repository: UserRepository,
    *,
    username: str,
    password: str,
    role: UserRole,
) -> str:
    """Create the account, or accept an identical existing one.

    Returns ``"created"`` when a new row is written and ``"exists"`` when an
    active user with the same credentials is already present. Any other
    pre-existing state (different password, different role, disabled user)
    is a conflict rather than something to silently paper over.
    """
    existing = await repository.find_by_username(username)
    if existing is not None:
        matches = (
            password_service.verify(password, existing.password_hash)
            and existing.role == role
            and existing.is_active
        )
        if not matches:
            raise RuntimeError(
                "bootstrap credentials do not match the existing user "
                f"{username!r}"
            )
        return "exists"
    await repository.add(
        User(
            username=username,
            password_hash=password_service.hash(password),
            role=role,
            is_active=True,
        )
    )
    return "created"


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
            outcome = await ensure_bootstrap_user(
                SqlAlchemyUserRepository(session),
                username=username,
                password=password,
                role=role,
            )
            await session.commit()
    finally:
        await engine.dispose()
    if outcome == "exists":
        print(f"BOOTSTRAP_USER_EXISTS username={username} role={role.value}")
    else:
        print(f"BOOTSTRAP_USER_CREATED username={username} role={role.value}")


def main() -> None:
    asyncio.run(create_bootstrap_user())


if __name__ == "__main__":
    main()
