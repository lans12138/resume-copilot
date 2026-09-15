"""Async JSON transport with error classification and bounded retry (FIN-008).

Every remote model call goes through :class:`JsonTransport`. It exists so the
retry and classification policy lives in exactly one place instead of being
re-derived (and subtly mis-derived) at each call site.

The central distinction is **retryable vs permanent**:

* retryable — the same request may succeed later. Covers connection failures,
  timeouts, HTTP 429 and HTTP 5xx.
* permanent — the same request will fail identically forever. Covers every other
  4xx, a body that is not JSON, and an HTTP 200 whose payload does not match the
  caller's schema.

That split is what lets the Celery layer decide whether to burn another attempt.
A 401 from a bad API key is permanent: retrying it just burns the retry budget
that a genuine 503 would need. Conversely a 429 must never be treated as
permanent, or a rate-limited run silently drops work.

Retries are bounded and use exponential backoff with jitter, honouring a
``Retry-After`` header when the server sends one. Jitter matters because several
workers rate-limited by the same upstream would otherwise retry in lockstep and
re-trigger the limit.

The transport never logs credentials. It logs the URL *path* and the model name,
never the full URL (which may embed a key) and never request headers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Mapping
from typing import Any, Protocol

import httpx2
from pydantic import ValidationError

logger = logging.getLogger(__name__)

# Upper bound on a server-provided Retry-After, so a hostile or buggy upstream
# cannot park a worker for an unbounded time.
MAX_RETRY_AFTER_SECONDS = 60.0


class TransportError(Exception):
    """Base class for transport failures.

    ``retryable`` is the single bit the caller needs: it answers "may the same
    request succeed if sent again?" without the caller having to know which
    status code or exception produced the failure.
    """

    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts


class RetryableTransportError(TransportError):
    """The request may succeed if sent again (timeout, 429, 5xx, connect error)."""

    retryable = True


class PermanentTransportError(TransportError):
    """The request will fail identically forever (4xx, malformed body, bad schema)."""

    retryable = False


class ResponseSchemaError(PermanentTransportError):
    """HTTP 200 whose payload did not match the expected shape.

    Permanent on purpose: a schema mismatch means our code and the upstream
    contract disagree, and resending the identical request cannot reconcile that.
    """

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message, status_code=200)
        self.details = details


class JsonSender(Protocol):
    """Minimal seam over ``httpx2`` so tests can inject a fake transport.

    Only the one operation the transport needs is modelled, which keeps the
    protocol satisfiable by a tiny test double with no HTTP machinery.
    """

    async def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> tuple[int, Mapping[str, str], Any]:
        """Return ``(status_code, response_headers, decoded_body)``."""
        ...


class RawResponseBody:
    """A response body that could not be decoded as JSON.

    Kept as a distinct type, not a dict, so it cannot accidentally satisfy an
    object-shaped schema check. ``parse_json_object`` rejects it explicitly.
    """

    __slots__ = ("status_code", "text")

    def __init__(self, *, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def __repr__(self) -> str:
        # Deliberately excludes the text: this object may be logged when a
        # response is unusable, and the body could echo request content.
        return f"RawResponseBody(status_code={self.status_code}, text_length={len(self.text)})"


class HttpxJsonSender:
    """``JsonSender`` backed by a real ``httpx2`` async client."""

    def __init__(self, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        # ``transport`` is injectable so tests can pass a MockTransport and
        # exercise the full client path without touching the network.
        self._transport = transport

    async def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> tuple[int, Mapping[str, str], Any]:
        async with httpx2.AsyncClient(transport=self._transport, timeout=timeout) as client:
            response = await client.post(url, json=dict(payload), headers=dict(headers))
        body: Any
        try:
            body = response.json()
        except ValueError:
            # A non-JSON body is surfaced as a distinct type rather than a dict
            # that merely looks like JSON. Wrapping it as ``{"_raw_text": ...}``
            # would satisfy an "is it an object?" check and let an HTML error page
            # be consumed as if it were a valid payload.
            body = RawResponseBody(status_code=response.status_code, text=response.text[:2000])
        return response.status_code, dict(response.headers), body


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Parse a bounded ``Retry-After`` delay in seconds.

    Only the delta-seconds form is honoured. The HTTP-date form is skipped rather
    than parsed: a skewed clock would silently turn it into a huge or negative
    delay, and the backoff schedule is a safe fallback.
    """
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _classify_status(status_code: int, body: Any) -> TransportError | None:
    """Map an HTTP status to the matching error, or ``None`` when it is a success."""
    if 200 <= status_code < 300:
        return None
    if status_code == 429:
        return RetryableTransportError(
            f"upstream rate limited the request (HTTP {status_code})",
            status_code=status_code,
        )
    if status_code >= 500:
        return RetryableTransportError(
            f"upstream failed the request (HTTP {status_code})",
            status_code=status_code,
        )
    return PermanentTransportError(
        f"upstream rejected the request (HTTP {status_code})",
        status_code=status_code,
    )


class JsonTransport:
    """POST JSON with classification, bounded retry and backoff.

    A single instance is cheap and stateless between calls, so it can be built
    per task invocation alongside the gateway.
    """

    def __init__(
        self,
        sender: JsonSender,
        *,
        max_attempts: int = 3,
        base_backoff_seconds: float = 0.5,
        sleep: Any = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._sender = sender
        self._max_attempts = max_attempts
        self._base_backoff_seconds: float = base_backoff_seconds
        # Seam for tests: swapping the sleep avoids real waiting in the retry
        # matrix. ``asyncio.sleep`` is the production value.
        self._sleep = sleep if sleep is not None else asyncio.sleep

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def _backoff_seconds(self, attempt_no: int, headers: Mapping[str, str]) -> float:
        """Delay before attempt ``attempt_no + 1``.

        A server-provided ``Retry-After`` wins because it encodes the upstream's
        own recovery estimate. Otherwise exponential backoff with full jitter.

        The jitter is a uniform draw over ``[0.5, 1.0)`` of the exponential
        delay rather than a symmetric one: retries are already the slow path, so
        the draw is never allowed to *extend* the wait beyond the computed
        exponential. Full jitter decorrelates concurrent workers, which is the
        point — a fleet that retries in lockstep re-creates the thundering herd
        that caused the 429 in the first place.
        """
        retry_after = _retry_after_seconds(headers)
        if retry_after is not None:
            return retry_after
        exponential = self._base_backoff_seconds * (2**attempt_no)
        jitter = 0.5 + random.random() / 2
        return float(exponential * jitter)

    async def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> Any:
        """Send a JSON body and return the decoded response body.

        Raises :class:`RetryableTransportError` or
        :class:`PermanentTransportError`; never returns on a non-2xx status.
        """
        request_headers = {"content-type": "application/json", **(headers or {})}
        last_error: TransportError | None = None

        for attempt_no in range(self._max_attempts):
            attempts_made = attempt_no + 1
            # Rebound every iteration, and left empty on the network-exception
            # path, so a server-provided Retry-After can never be carried over
            # from an earlier attempt.
            response_headers: Mapping[str, str] = {}
            try:
                status_code, response_headers, body = await self._sender.post_json(
                    url, payload, headers=request_headers, timeout=timeout
                )
            except httpx2.TimeoutException as error:
                last_error = RetryableTransportError(f"upstream timed out: {error}")
            except httpx2.TransportError as error:
                # Covers connect/read/write/protocol errors: all are network-level
                # and therefore worth another attempt.
                last_error = RetryableTransportError(f"upstream connection failed: {error}")
            else:
                error_from_status = _classify_status(status_code, body)
                if error_from_status is None:
                    return body
                last_error = error_from_status

            if not last_error.retryable or attempts_made >= self._max_attempts:
                last_error.attempts = attempts_made
                logger.warning(
                    "model_transport_failed",
                    extra={
                        "path": _safe_path(url),
                        "status_code": last_error.status_code,
                        "retryable": last_error.retryable,
                        "attempts": attempts_made,
                        "error": str(last_error),
                    },
                )
                raise last_error

            delay = self._backoff_seconds(attempt_no, response_headers)
            logger.warning(
                "model_transport_retrying",
                extra={
                    "path": _safe_path(url),
                    "status_code": last_error.status_code,
                    "attempt": attempts_made,
                    "delay_seconds": round(delay, 3),
                },
            )
            await self._sleep(delay)

        # Unreachable: the loop either returns or raises. Kept so the function
        # has no implicit ``None`` return under mypy's strict analysis.
        raise AssertionError("retry loop exited without returning or raising")


def _safe_path(url: str) -> str:
    """Return only the URL path, dropping the host and any embedded credentials.

    A base URL can carry a key in its userinfo or query string, so the full URL
    never reaches a log sink.
    """
    parsed = httpx2.URL(url)
    return parsed.path or "/"


def parse_json_object(body: Any) -> Mapping[str, Any]:
    """Validate that a decoded body is a JSON object.

    Raises :class:`ResponseSchemaError` (permanent) when it is not, so a
    malformed 200 is never retried.
    """
    if isinstance(body, RawResponseBody):
        raise ResponseSchemaError(
            "upstream returned a non-JSON body",
            details={"text_length": len(body.text)},
        )
    if isinstance(body, Mapping):
        return body
    if isinstance(body, str):
        # Some gateways return a JSON document with a text/plain content type, so
        # a string body is decoded once before being rejected.
        try:
            decoded = json.loads(body)
        except ValueError as error:
            raise ResponseSchemaError("upstream returned a non-JSON body") from error
        if isinstance(decoded, Mapping):
            return decoded
    raise ResponseSchemaError("upstream returned a JSON payload that is not an object")


def validate_response(model: type[Any], body: Any) -> Any:
    """Validate a decoded body against a Pydantic model.

    Kept here rather than at each call site so that "a 200 with the wrong shape is
    permanent, not retryable" is enforced once, consistently.
    """
    try:
        return model.model_validate(body)
    except ValidationError as error:
        raise ResponseSchemaError(
            "upstream response did not match the expected schema",
            details={"errors": error.error_count()},
        ) from error


__all__ = [
    "HttpxJsonSender",
    "JsonSender",
    "JsonTransport",
    "PermanentTransportError",
    "RawResponseBody",
    "ResponseSchemaError",
    "RetryableTransportError",
    "TransportError",
    "parse_json_object",
    "validate_response",
]
