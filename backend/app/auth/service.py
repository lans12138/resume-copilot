"""Authentication policy independent of HTTP and SQLAlchemy."""

from __future__ import annotations

from datetime import UTC, datetime

from backend.app.auth.models import User
from backend.app.auth.passwords import PasswordService, password_service
from backend.app.auth.repository import UserRepository, normalize_username


class InvalidCredentials(ValueError):
    """Deliberately gives no indication which credential failed."""


class AuthenticationService:
    def __init__(
        self,
        users: UserRepository,
        passwords: PasswordService = password_service,
    ) -> None:
        self._users = users
        self._passwords = passwords

    async def authenticate(self, username: str, password: str) -> User:
        normalized_username = normalize_username(username)
        user = await self._users.find_by_username(normalized_username)
        if user is None:
            self._passwords.verify_unknown_user(password)
            raise InvalidCredentials

        try:
            password_valid = self._passwords.verify(password, user.password_hash)
        except Exception as error:
            raise InvalidCredentials from error
        if not password_valid or not user.is_active:
            raise InvalidCredentials

        user.last_login_at = datetime.now(UTC)
        return user
