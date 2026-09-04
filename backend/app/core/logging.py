"""Structured JSON logging with context injection and recursive redaction."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from backend.app.core.context import get_request_context, get_run_context
from backend.app.core.settings import Settings

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_PARTS = (
    "password",
    "authorization",
    "access_token",
    "api_key",
    "secret",
    "resume_text",
    "document_binary",
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_API_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

_STANDARD_LOG_RECORD_FIELDS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime"}


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold()
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact_value(value: Any, *, key: str | None = None) -> Any:
    """Recursively remove secrets and common contact markers from log values."""
    if key is not None and _is_sensitive_key(key):
        return REDACTED

    if isinstance(value, Mapping):
        return {
            str(item_key): redact_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item) for item in value]

    if isinstance(value, bytes | bytearray):
        return REDACTED

    if isinstance(value, str):
        redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
        redacted = _API_KEY_PATTERN.sub(REDACTED, redacted)
        redacted = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", redacted)
        return _PHONE_PATTERN.sub("[REDACTED_PHONE]", redacted)

    if value is None or isinstance(value, bool | int | float):
        return value

    return redact_value(str(value))


_SCAN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", _EMAIL_PATTERN),
    ("phone", _PHONE_PATTERN),
    ("bearer_token", _BEARER_PATTERN),
    ("api_key", _API_KEY_PATTERN),
)


def scan_for_leaks(text: object) -> list[str]:
    """Report which sensitive markers remain in text, for CI log scanning (§18.3).

    Returns the matched categories (e.g. ``["email", "api_key"]``). An empty list
    means no known sensitive marker was detected, i.e. the log line is clean.
    """
    if not isinstance(text, str):
        text = str(text)
    found: list[str] = []
    for category, pattern in _SCAN_PATTERNS:
        if pattern.search(text):
            found.append(category)
    return found


class JsonLogFormatter(logging.Formatter):
    """Render one bounded JSON object per log record."""

    def __init__(self, *, service: str, environment: str) -> None:
        super().__init__()
        self._service = service
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        context = get_request_context()
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat().replace(
                "+00:00", "Z"
            ),
            "level": record.levelname,
            "service": self._service,
            "environment": self._environment,
            "message": redact_value(record.getMessage()),
        }

        if context is not None:
            payload.update(
                {
                    key: value
                    for key, value in {
                        "request_id": context.request_id,
                        "actor_id": context.actor_id,
                        "role": context.role,
                        "client_ip": context.client_ip,
                        "user_agent": context.user_agent,
                    }.items()
                    if value is not None
                }
            )

        run_context = get_run_context()
        if run_context is not None:
            payload.update(
                {
                    key: value
                    for key, value in asdict(run_context).items()
                    if value is not None
                }
            )

        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = redact_value(value, key=key)

        if record.exc_info is not None:
            payload["exception"] = redact_value(self.formatException(record.exc_info))

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(settings: Settings) -> None:
    """Install one process-wide structured handler without duplicating handlers."""
    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level)

    for handler in tuple(root_logger.handlers):
        if getattr(handler, "_resume_copilot_handler", False):
            root_logger.removeHandler(handler)

    handler = logging.StreamHandler()
    handler._resume_copilot_handler = True  # type: ignore[attr-defined]
    if settings.log_format.value == "json":
        handler.setFormatter(
            JsonLogFormatter(service=settings.app_name, environment=settings.app_env.value)
        )
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root_logger.addHandler(handler)
