"""Structured logging context and redaction tests."""

import json
import logging

from backend.app.core.context import RequestContext, get_request_context, request_context_scope
from backend.app.core.logging import REDACTED, JsonLogFormatter, redact_value


def test_recursive_redaction_filters_sensitive_keys_and_contact_markers() -> None:
    value = {
        "password": "not-for-logs",
        "nested": {
            "api_key": "sk-test-sensitive-value",
            "message": "alice@example.com 13800138000 Bearer abc.def.ghi",
        },
        "document_binary": b"binary-content",
    }

    redacted = redact_value(value)
    serialized = json.dumps(redacted)

    assert redacted["password"] == REDACTED
    assert redacted["document_binary"] == REDACTED
    assert "not-for-logs" not in serialized
    assert "sk-test-sensitive-value" not in serialized
    assert "alice@example.com" not in serialized
    assert "13800138000" not in serialized
    assert "abc.def.ghi" not in serialized


def test_json_formatter_injects_context_and_redacts_record_extras() -> None:
    formatter = JsonLogFormatter(service="resume-copilot", environment="test")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="contact alice@example.com",
        args=(),
        exc_info=None,
    )
    record.__dict__["authorization"] = "Bearer private-token"
    record.__dict__["safe_field"] = "visible"

    with request_context_scope(
        RequestContext(
            request_id="request-logging-123",
            actor_id="actor-1",
            role="HR",
            client_ip="127.0.0.1",
        )
    ):
        payload = json.loads(formatter.format(record))

    assert payload["request_id"] == "request-logging-123"
    assert payload["actor_id"] == "actor-1"
    assert payload["authorization"] == REDACTED
    assert payload["safe_field"] == "visible"
    assert "alice@example.com" not in payload["message"]
    assert get_request_context() is None
