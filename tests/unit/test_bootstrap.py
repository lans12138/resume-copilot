"""Unit tests for the idempotent bootstrap account semantics (FIN-013).

``start_stack.ps1`` bootstraps the demo account *after* seeding, and the
seed script already provisions the same account. The bootstrap step must
therefore accept an identical existing user instead of failing the
one-command start.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from backend.app.auth.bootstrap import ensure_bootstrap_user
from backend.app.auth.models import User, UserRole
from backend.app.auth.passwords import password_service
from backend.app.auth.repository import InMemoryUserRepository, normalize_username

DEMO_USERNAME = "hr.demo"
DEMO_PASSWORD = "demo-password-123"


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive a coroutine to completion (no anyio pytest plugin is installed)."""
    return asyncio.run(coro)


def _existing_user(
    *,
    password: str = DEMO_PASSWORD,
    role: UserRole = UserRole.HR,
    is_active: bool = True,
) -> User:
    return User(
        username=normalize_username(DEMO_USERNAME),
        password_hash=password_service.hash(password),
        role=role,
        is_active=is_active,
    )


def test_creates_the_account_when_missing() -> None:
    repository = InMemoryUserRepository()

    outcome = _run(
        ensure_bootstrap_user(
            repository,
            username=DEMO_USERNAME,
            password=DEMO_PASSWORD,
            role=UserRole.HR,
        )
    )

    assert outcome == "created"
    created = _run(repository.find_by_username(DEMO_USERNAME))
    assert created is not None
    assert created.role == UserRole.HR
    assert created.is_active
    assert password_service.verify(DEMO_PASSWORD, created.password_hash)


def test_skips_an_identical_existing_account() -> None:
    repository = InMemoryUserRepository([_existing_user()])

    outcome = _run(
        ensure_bootstrap_user(
            repository,
            username=DEMO_USERNAME,
            password=DEMO_PASSWORD,
            role=UserRole.HR,
        )
    )

    assert outcome == "exists"


def test_rejects_a_password_mismatch() -> None:
    repository = InMemoryUserRepository([_existing_user(password="a-different-passphrase")])

    with pytest.raises(RuntimeError, match="do not match"):
        _run(
            ensure_bootstrap_user(
                repository,
                username=DEMO_USERNAME,
                password=DEMO_PASSWORD,
                role=UserRole.HR,
            )
        )


def test_rejects_a_role_mismatch() -> None:
    repository = InMemoryUserRepository([_existing_user(role=UserRole.ADMIN)])

    with pytest.raises(RuntimeError, match="do not match"):
        _run(
            ensure_bootstrap_user(
                repository,
                username=DEMO_USERNAME,
                password=DEMO_PASSWORD,
                role=UserRole.HR,
            )
        )


def test_rejects_a_disabled_existing_account() -> None:
    repository = InMemoryUserRepository([_existing_user(is_active=False)])

    with pytest.raises(RuntimeError, match="do not match"):
        _run(
            ensure_bootstrap_user(
                repository,
                username=DEMO_USERNAME,
                password=DEMO_PASSWORD,
                role=UserRole.HR,
            )
        )


def test_rejecting_a_conflict_writes_nothing() -> None:
    repository = InMemoryUserRepository([_existing_user(role=UserRole.ADMIN)])

    with pytest.raises(RuntimeError):
        _run(
            ensure_bootstrap_user(
                repository,
                username=DEMO_USERNAME,
                password=DEMO_PASSWORD,
                role=UserRole.HR,
            )
        )

    stored = _run(repository.find_by_username(DEMO_USERNAME))
    assert stored is not None
    assert stored.role == UserRole.ADMIN
