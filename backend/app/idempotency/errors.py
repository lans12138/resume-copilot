"""Idempotency-specific errors (FIN-001).

``IdempotencyKeyReusedError`` is an :class:`AppError` so the existing exception
handler maps it to a stable 409 contract. ``IdempotencyReplay`` carries a frozen
response that the API layer replays verbatim (status + body), so a retried
request gets the *original* result rather than re-executing the handler.
"""

from __future__ import annotations

from typing import Any

from backend.app.core.errors import AppError


class IdempotencyKeyReusedError(AppError):
    """Same ``Idempotency-Key`` reused for a different request body."""

    def __init__(self) -> None:
        super().__init__(
            code="IDEMPOTENCY_KEY_REUSED",
            http_status=409,
            safe_message="Idempotency-Key 已被用于不同的请求，请更换 Key 后重试",
            retryable=False,
        )


class IdempotencyReplay(Exception):
    """Raised to replay a previously stored successful response."""

    def __init__(self, *, status: int, body: dict[str, Any]) -> None:
        super().__init__("idempotency replay")
        self.status = status
        self.body = body
