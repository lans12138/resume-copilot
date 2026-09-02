"""Password, token, authentication, and role policy tests."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest

from backend.app.auth.dependencies import ensure_role
from backend.app.auth.models import User, UserRole
from backend.app.auth.passwords import PasswordService
from backend.app.auth.repository import InMemoryUserRepository, normalize_username
from backend.app.auth.service import AuthenticationService, InvalidCredentials
from backend.app.auth.tokens import Actor, InvalidAccessToken, TokenService
from backend.app.core.errors import AppError
from tests.unit.settings_factory import make_settings


def make_user(
    *,
    username: str = "hr-demo",
    role: UserRole = UserRole.HR,
    is_active: bool = True,
    password: str = "correct horse battery staple",
) -> User:
    return User(
        id=uuid4(),
        username=username,
        password_hash=PasswordService().hash(password),
        role=role,
        is_active=is_active,
    )


def test_argon2_hashes_use_random_salts_and_verify() -> None:
    passwords = PasswordService()

    first = passwords.hash("same password")
    second = passwords.hash("same password")

    assert first.startswith("$argon2")
    assert first != second
    assert passwords.verify("same password", first) is True
    assert passwords.verify("wrong password", first) is False


def test_username_normalization_is_stable() -> None:
    assert normalize_username("  ＨＲ-User  ") == "hr-user"


def test_authentication_normalizes_username_and_updates_last_login() -> None:
    user = make_user()
    service = AuthenticationService(InMemoryUserRepository([user]))

    authenticated = asyncio.run(
        service.authenticate("  HR-DEMO ", "correct horse battery staple")
    )

    assert authenticated is user
    assert user.last_login_at is not None


@pytest.mark.parametrize(
    ("username", "password", "active"),
    [
        ("missing", "wrong password", True),
        ("hr-demo", "wrong password", True),
        ("hr-demo", "correct horse battery staple", False),
    ],
)
def test_invalid_and_disabled_accounts_share_one_failure(
    username: str,
    password: str,
    active: bool,
) -> None:
    user = make_user(is_active=active)
    service = AuthenticationService(InMemoryUserRepository([user]))

    with pytest.raises(InvalidCredentials):
        asyncio.run(service.authenticate(username, password))


def test_access_token_contains_only_the_approved_claims() -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    jti = UUID("11111111-1111-4111-8111-111111111111")
    settings = make_settings(access_token_ttl_minutes=30)
    service = TokenService(settings, clock=lambda: now, jti_factory=lambda: jti)
    user = make_user()

    token = service.issue(user)
    assert settings.jwt_secret is not None
    claims = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
        options={"verify_exp": False, "verify_iat": False},
    )

    assert set(claims) == {"sub", "role", "iat", "exp", "jti"}
    assert claims["sub"] == str(user.id)
    assert claims["role"] == "HR"
    assert claims["jti"] == str(jti)
    assert claims["exp"] - claims["iat"] == 1800


def test_expired_access_token_is_rejected() -> None:
    old = datetime.now(UTC) - timedelta(hours=2)
    service = TokenService(make_settings(access_token_ttl_minutes=1), clock=lambda: old)

    with pytest.raises(InvalidAccessToken):
        service.decode(service.issue(make_user()))


def test_role_policy_allows_only_explicit_roles() -> None:
    actor = Actor(user_id=uuid4(), username="manager", role=UserRole.HIRING_MANAGER)

    ensure_role(actor, UserRole.HR, UserRole.HIRING_MANAGER)
    with pytest.raises(AppError) as error:
        ensure_role(actor, UserRole.HR)

    assert error.value.http_status == 403
    assert error.value.code == "FORBIDDEN"
