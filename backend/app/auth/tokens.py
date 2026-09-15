"""Strict access-token creation and validation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import jwt

from backend.app.auth.models import User, UserRole
from backend.app.core.settings import Settings

_ALLOWED_CLAIMS = frozenset({"sub", "role", "iat", "exp", "jti"})


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: UUID
    username: str
    role: UserRole


class InvalidAccessToken(ValueError):
    """Raised when an access token violates the application claim contract."""


class TokenService:
    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
        jti_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        assert settings.jwt_secret is not None
        self._secret = settings.jwt_secret.get_secret_value()
        self._algorithm = settings.jwt_algorithm
        self._ttl = timedelta(minutes=settings.access_token_ttl_minutes)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._jti_factory = jti_factory

    @property
    def expires_in_seconds(self) -> int:
        return int(self._ttl.total_seconds())

    def issue(self, user: User) -> str:
        issued_at = self._clock()
        claims = {
            "sub": str(user.id),
            "role": user.role.value,
            "iat": issued_at,
            "exp": issued_at + self._ttl,
            "jti": str(self._jti_factory()),
        }
        return jwt.encode(claims, self._secret, algorithm=self._algorithm)

    def decode(self, token: str) -> tuple[UUID, UserRole]:
        try:
            decoded = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                options={"require": list(_ALLOWED_CLAIMS)},
            )
            claims = cast(dict[str, object], decoded)
            if frozenset(claims) != _ALLOWED_CLAIMS:
                raise InvalidAccessToken("unexpected JWT claims")
            return UUID(str(claims["sub"])), UserRole(str(claims["role"]))
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as error:
            raise InvalidAccessToken("invalid access token") from error
