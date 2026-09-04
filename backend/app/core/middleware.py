"""Pure ASGI middleware for request correlation and access logging."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import MutableMapping

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.app.core.context import (
    RequestContext,
    reset_request_context,
    set_request_context,
)
from backend.app.core.errors import handle_unexpected_error
from backend.app.core.metrics import (
    http_auth_failures_total,
    http_conflict_total,
    http_request_duration_seconds,
    http_requests_total,
    normalize_route,
)

logger = logging.getLogger(__name__)

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")


def new_request_id() -> str:
    """Create a non-guessable request correlation identifier."""
    return f"req_{uuid.uuid4().hex}"


def normalize_request_id(candidate: str | None) -> str:
    """Accept a bounded safe upstream ID or replace it with a server-generated ID."""
    if candidate is not None:
        stripped = candidate.strip()
        if _REQUEST_ID_PATTERN.fullmatch(stripped) is not None:
            return stripped
    return new_request_id()


class RequestContextMiddleware:
    """Bind request context, expose its header, and emit completion logs."""

    def __init__(self, app: ASGIApp, *, request_id_header: str) -> None:
        self._application = app
        self._request_id_header = request_id_header

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._application(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = normalize_request_id(headers.get(self._request_id_header))
        client = scope.get("client")
        context = RequestContext(
            request_id=request_id,
            client_ip=client[0] if client is not None else None,
            user_agent=headers.get("user-agent"),
        )
        token = set_request_context(context)
        started_at = time.perf_counter()
        status_code = 500

        state = scope.setdefault("state", {})
        if isinstance(state, MutableMapping):
            state["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                response_headers = MutableHeaders(scope=message)
                response_headers[self._request_id_header] = request_id
            await send(message)

        logger.info(
            "http_request_started",
            extra={"method": scope["method"], "path": scope["path"]},
        )
        try:
            await self._application(scope, receive, send_with_request_id)
        except Exception as error:
            response = await handle_unexpected_error(Request(scope, receive), error)
            await response(scope, receive, send_with_request_id)
        finally:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 3)
            logger.info(
                "http_request_completed",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
            reset_request_context(token)


class MetricsMiddleware:
    """Record per-request counts, durations, auth failures and conflicts (IMP-029)."""

    def __init__(self, app: ASGIApp) -> None:
        self._application = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._application(scope, receive, send)
            return

        method = scope["method"]
        route = normalize_route(scope["path"])
        started_at = time.perf_counter()
        status_code = 500

        async def send_with_metrics(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        await self._application(scope, receive, send_with_metrics)

        duration = time.perf_counter() - started_at
        http_requests_total().inc(method=method, route=route, status=str(status_code))
        http_request_duration_seconds().record(duration, route=route, method=method)
        if status_code in (401, 403):
            http_auth_failures_total().inc(method=method)
        elif status_code == 409:
            http_conflict_total().inc(method=method)
