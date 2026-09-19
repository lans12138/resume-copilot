"""FIN-008: HTTP transport classification and bounded retry.

Everything here runs against ``httpx2.MockTransport`` or a scripted sender, so the
retry matrix is exercised through the real client path with no network and no
sleeping. Follows the repository convention of driving coroutines with
``asyncio.run`` rather than an async test plugin (none is installed).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine, Mapping
from typing import Any

import httpx2
import pytest

from backend.app.infrastructure.http_transport import (
    AttemptRecord,
    HttpxJsonSender,
    JsonTransport,
    PermanentTransportError,
    RawResponseBody,
    ResponseSchemaError,
    RetryableTransportError,
    TransportConnectionError,
    TransportTimeoutError,
    parse_json_object,
)


class Recorder:
    """A scripted sender that counts calls and replays a list of outcomes."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.delays: list[float] = []

    async def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, str], object]:
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


def _transport(recorder: Recorder, *, max_attempts: int = 3) -> JsonTransport:
    async def sleep(seconds: float) -> None:
        recorder.delays.append(seconds)

    return JsonTransport(recorder, max_attempts=max_attempts, base_backoff_seconds=1.0, sleep=sleep)


def _ok(body: object = None) -> tuple[int, dict[str, str], object]:
    return 200, {}, (body if body is not None else {"ok": True})


def _status(code: int, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], object]:
    return code, headers or {}, {"error": "upstream"}


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive a coroutine without an async plugin (none is installed here)."""
    return asyncio.run(coro)


async def _immediate(_seconds: float) -> None:
    """Retry delays are asserted, never waited on."""
    return None


def test_success_returns_the_body_without_retrying() -> None:
    recorder = Recorder([_ok({"value": 7})])
    result = _run(_transport(recorder).post_json("https://x.test/v1/chat", {}, timeout=5))
    assert result == {"value": 7}
    assert recorder.calls == 1
    assert recorder.delays == []


def test_rate_limit_is_retryable_and_then_succeeds() -> None:
    """429 must never be permanent: a rate-limited run would otherwise drop work."""
    recorder = Recorder([_status(429), _status(429), _ok()])
    result = _run(_transport(recorder).post_json("https://x.test/v1/chat", {}, timeout=5))
    assert result == {"ok": True}
    assert recorder.calls == 3
    assert len(recorder.delays) == 2


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_every_5xx_is_retryable(code: int) -> None:
    recorder = Recorder([_status(code), _ok()])
    result = _run(_transport(recorder).post_json("https://x.test/v1/chat", {}, timeout=5))
    assert result == {"ok": True}
    assert recorder.calls == 2


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_client_errors_are_permanent_and_not_retried(code: int) -> None:
    """A bad key or a malformed request fails identically forever.

    Retrying it would burn the budget a genuine transient failure needs, so the
    attempt count must stay at one.
    """
    recorder = Recorder([_status(code)])
    with pytest.raises(PermanentTransportError) as caught:
        _run(_transport(recorder).post_json("https://x.test/v1/chat", {}, timeout=5))
    assert caught.value.status_code == code
    assert caught.value.retryable is False
    assert recorder.calls == 1


def test_timeout_is_retryable() -> None:
    recorder = Recorder([httpx2.ReadTimeout("slow"), _ok()])
    result = _run(_transport(recorder).post_json("https://x.test/v1/chat", {}, timeout=5))
    assert result == {"ok": True}
    assert recorder.calls == 2


def test_timeout_raises_the_specific_timeout_subtype() -> None:
    """PORT-003 records a reason code per failed call, so the cause must be typed.

    Matching on the message text would silently misclassify the first time someone
    reworded it; the subtype is what makes TIMEOUT distinguishable from a 5xx.
    """
    recorder = Recorder([httpx2.ReadTimeout("slow")])
    with pytest.raises(TransportTimeoutError) as caught:
        _run(
            _transport(recorder, max_attempts=1).post_json(
                "https://x.test/v1/chat", {}, timeout=5
            )
        )
    assert caught.value.retryable is True


def test_connect_error_raises_the_specific_connection_subtype() -> None:
    recorder = Recorder([httpx2.ConnectError("refused")])
    with pytest.raises(TransportConnectionError):
        _run(
            _transport(recorder, max_attempts=1).post_json(
                "https://x.test/v1/chat", {}, timeout=5
            )
        )


def test_attempt_record_counts_a_successful_call() -> None:
    recorder = Recorder([_status(503), _ok()])
    record = AttemptRecord()
    _run(
        _transport(recorder).post_json(
            "https://x.test/v1/chat", {}, timeout=5, attempts=record
        )
    )
    assert record.attempts == 2


def test_attempt_record_is_written_on_the_raising_path_too() -> None:
    """ "This took 3 attempts and still failed" is exactly the number to keep."""
    recorder = Recorder([_status(503)])
    record = AttemptRecord()
    with pytest.raises(RetryableTransportError):
        _run(
            _transport(recorder, max_attempts=3).post_json(
                "https://x.test/v1/chat", {}, timeout=5, attempts=record
            )
        )
    assert record.attempts == 3


def test_attempt_records_are_per_request_not_per_transport() -> None:
    """A transport is shared across concurrent calls, so the counter cannot live on it.

    Two calls through one transport must not see each other's retries, or a
    concurrent run would attribute its neighbour's latency to its own output.
    """
    recorder = Recorder([_status(503), _ok(), _ok()])
    transport = _transport(recorder)
    first = AttemptRecord()
    second = AttemptRecord()
    _run(transport.post_json("https://x.test/v1/chat", {}, timeout=5, attempts=first))
    _run(transport.post_json("https://x.test/v1/chat", {}, timeout=5, attempts=second))
    assert first.attempts == 2
    assert second.attempts == 1


def test_connect_error_is_retryable() -> None:
    recorder = Recorder([httpx2.ConnectError("refused"), _ok()])
    _run(_transport(recorder).post_json("https://x.test/v1/chat", {}, timeout=5))
    assert recorder.calls == 2


def test_retry_stops_at_max_attempts_and_reports_the_count() -> None:
    """The retry budget is bounded; an endless 503 must not spin forever."""
    recorder = Recorder([_status(503)])
    with pytest.raises(RetryableTransportError) as caught:
        _run(
            _transport(recorder, max_attempts=4).post_json("https://x.test/v1/chat", {}, timeout=5)
        )
    assert recorder.calls == 4
    assert caught.value.attempts == 4
    assert caught.value.retryable is True


def test_retry_after_header_overrides_the_backoff_schedule() -> None:
    """A server's own recovery estimate wins over our exponential guess."""
    recorder = Recorder([_status(429, {"Retry-After": "7"}), _ok()])
    _run(_transport(recorder).post_json("https://x.test/v1/chat", {}, timeout=5))
    assert recorder.delays == [7.0]


def test_retry_after_is_capped_so_an_upstream_cannot_park_a_worker() -> None:
    recorder = Recorder([_status(429, {"Retry-After": "99999"}), _ok()])
    _run(_transport(recorder).post_json("https://x.test/v1/chat", {}, timeout=5))
    assert recorder.delays[0] <= 60.0


def test_retry_after_is_ignored_when_not_a_number() -> None:
    """The HTTP-date form is skipped rather than mis-parsed into a wrong delay."""
    recorder = Recorder([_status(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), _ok()])
    _run(_transport(recorder).post_json("https://x.test/v1/chat", {}, timeout=5))
    # Fell back to the exponential schedule (1.0 * 2**0, halved at minimum).
    assert 0.5 <= recorder.delays[0] <= 1.0


def test_backoff_grows_between_attempts() -> None:
    recorder = Recorder([_status(503)])
    with pytest.raises(RetryableTransportError):
        _run(
            _transport(recorder, max_attempts=3).post_json("https://x.test/v1/chat", {}, timeout=5)
        )
    # Two delays, and the second is drawn from a strictly larger range.
    assert len(recorder.delays) == 2
    assert recorder.delays[1] > recorder.delays[0]


def test_a_stale_retry_after_does_not_leak_into_a_later_attempt() -> None:
    """A network failure after a header-bearing response must reset the header.

    Otherwise a Retry-After from attempt one would silently govern attempt three.
    """
    recorder = Recorder([_status(429, {"Retry-After": "5"}), httpx2.ReadTimeout("slow"), _ok()])
    _run(_transport(recorder).post_json("https://x.test/v1/chat", {}, timeout=5))
    assert recorder.delays[0] == 5.0
    assert recorder.delays[1] != 5.0


def test_request_headers_include_content_type_and_the_caller_values() -> None:
    seen: dict[str, object] = {}

    class Sender:
        async def post_json(
            self,
            url: str,
            payload: Mapping[str, Any],
            *,
            headers: Mapping[str, str],
            timeout: float,
        ) -> tuple[int, dict[str, str], object]:
            seen["headers"] = dict(headers)
            seen["url"] = url
            seen["timeout"] = timeout
            return _ok()

    transport = JsonTransport(Sender())
    _run(
        transport.post_json(
            "https://x.test/v1/chat", {"a": 1}, headers={"authorization": "Bearer k"}, timeout=9
        )
    )
    assert seen["headers"] == {"content-type": "application/json", "authorization": "Bearer k"}
    assert seen["timeout"] == 9


def test_zero_max_attempts_is_rejected_at_construction() -> None:
    """A zero budget would mean a request that can never succeed."""

    class Sender:
        async def post_json(
            self,
            url: str,
            payload: Mapping[str, Any],
            *,
            headers: Mapping[str, str],
            timeout: float,
        ) -> tuple[int, dict[str, str], object]:
            raise AssertionError("must not be called")

    with pytest.raises(ValueError):
        JsonTransport(Sender(), max_attempts=0)


def test_mock_transport_drives_the_real_client_path() -> None:
    """The retry logic must work over the actual httpx2 client, not only a stub."""
    attempts = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx2.Response(503, json={"error": "busy"}, headers={"Retry-After": "0"})
        return httpx2.Response(200, json={"ok": True})

    async def main() -> object:
        transport = JsonTransport(
            HttpxJsonSender(transport=httpx2.MockTransport(handler)),
            max_attempts=5,
            sleep=_immediate,
        )
        return await transport.post_json("https://x.test/v1/chat", {"a": 1}, timeout=5)

    assert _run(main()) == {"ok": True}
    assert attempts["n"] == 3


def test_mock_transport_surfaces_a_permanent_error_without_retrying() -> None:
    attempts = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts["n"] += 1
        return httpx2.Response(401, json={"error": "bad key"})

    async def main() -> None:
        transport = JsonTransport(
            HttpxJsonSender(transport=httpx2.MockTransport(handler)),
            max_attempts=5,
            sleep=_immediate,
        )
        await transport.post_json("https://x.test/v1/chat", {}, timeout=5)

    with pytest.raises(PermanentTransportError):
        _run(main())
    assert attempts["n"] == 1


def test_parse_json_object_accepts_a_json_encoded_string_body() -> None:
    """Some gateways label a JSON document as text/plain; decode it once."""
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_json_object_rejects_a_non_object() -> None:
    with pytest.raises(ResponseSchemaError):
        parse_json_object([1, 2, 3])


def test_parse_json_object_rejects_unparseable_text() -> None:
    with pytest.raises(ResponseSchemaError):
        parse_json_object("not json")


def test_schema_error_is_permanent() -> None:
    """A 200 with the wrong shape cannot be fixed by resending the same request."""
    assert ResponseSchemaError("x").retryable is False
    assert isinstance(ResponseSchemaError("x"), PermanentTransportError)


def test_non_json_200_body_is_surfaced_for_the_caller_to_reject() -> None:
    """An HTML error page must not pass an object-shaped schema check."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>gateway</html>")

    async def main() -> object:
        transport = JsonTransport(HttpxJsonSender(transport=httpx2.MockTransport(handler)))
        return await transport.post_json("https://x.test/v1/chat", {}, timeout=5)

    body = _run(main())
    # It is surfaced as a distinct type rather than a dict that mimics JSON...
    assert isinstance(body, RawResponseBody)
    assert "gateway" in body.text
    # ...so the caller-side guard rejects it as permanent, not as a valid object.
    with pytest.raises(ResponseSchemaError):
        parse_json_object(body)


def test_raw_body_repr_does_not_include_the_payload() -> None:
    """A logged unusable response must not echo its body."""
    body = RawResponseBody(status_code=200, text="secret-echo-here")
    assert "secret-echo-here" not in repr(body)
    assert "text_length=16" in repr(body)


def test_transport_never_logs_the_credential() -> None:
    """A URL can carry a key in its userinfo or query, so only the path is logged.

    The fields are attached via ``extra``, so they are not in the rendered message
    and ``caplog.text`` alone would miss a leak. A handler is attached directly and
    every record attribute is scanned.
    """
    secret_url = "https://user:sk-leak-me@api.test/v1/v1/chat?api_key=sk-also-secret"
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("backend.app.infrastructure.http_transport")
    handler_obj = Capture()
    logger.addHandler(handler_obj)
    previous_level = logger.level
    logger.setLevel(logging.WARNING)

    try:

        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(500, json={"error": "boom"})

        async def main() -> None:
            transport = JsonTransport(
                HttpxJsonSender(transport=httpx2.MockTransport(handler)),
                max_attempts=2,
                sleep=_immediate,
            )
            await transport.post_json(secret_url, {}, timeout=5)

        with pytest.raises(RetryableTransportError):
            _run(main())
    finally:
        logger.removeHandler(handler_obj)
        logger.setLevel(previous_level)

    assert records, "the transport must log a classified failure"
    # Every attribute of every record, plus the rendered line, is scanned: a
    # credential could hide in ``extra`` where a text-only check would miss it.
    scanned = " ".join(
        [record.getMessage() for record in records]
        + [f"{key}={value}" for record in records for key, value in vars(record).items()]
    )
    assert "sk-leak-me" not in scanned
    assert "sk-also-secret" not in scanned
    assert "user:" not in scanned
    assert "api.test" not in scanned
    # ...while still recording something diagnostically useful.
    assert any(getattr(record, "path", None) for record in records)
