"""Argon2 password hashing with a reusable fake-user verification hash."""

from pwdlib import PasswordHash


class PasswordService:
    def __init__(self) -> None:
        self._hasher = PasswordHash.recommended()
        self._dummy_hash = self._hasher.hash("not-a-real-account-password")

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password: str, password_hash: str) -> bool:
        return self._hasher.verify(password, password_hash)

    def verify_unknown_user(self, password: str) -> None:
        """Consume an Argon2 verification for usernames absent from the database."""
        self._hasher.verify(password, self._dummy_hash)


password_service = PasswordService()
