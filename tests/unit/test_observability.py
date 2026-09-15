"""Unit tests for IMP-029 observability primitives (metrics, redaction, run context)."""

from __future__ import annotations

import json
import logging
import time

import pytest

from backend.app.core.context import RunContext, reset_run_context, set_run_context
from backend.app.core.logging import JsonLogFormatter, redact_value, scan_for_leaks
from backend.app.core.metrics import (
    Counter,
    Histogram,
    MetricsRegistry,
    Timer,
    get_registry,
    normalize_route,
)


def test_counter_increments_and_snapshots() -> None:
    counter = Counter("test_counter", "a counter for tests")
    counter.inc()
    counter.inc(2.5)
    assert counter.snapshot() == {"": 3.5}


def test_histogram_computes_quantiles() -> None:
    hist = Histogram("test_hist")
    for value in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 100]:
        hist.record(value)
    summary = hist.snapshot()[""]
    assert summary["count"] == 11
    assert summary["sum"] == 155.0
    assert summary["p50"] == 6
    assert summary["p95"] == 55  # 0.95 * (11-1) = 9.5 -> midpoint of 10 and 100
    assert summary["p99"] == pytest.approx(91)
    assert summary["p95"] is not None


def test_timer_records_duration_into_histogram() -> None:
    registry = MetricsRegistry()
    hist = registry.histogram("timer_hist")
    with Timer(hist):
        time.sleep(0.002)
    assert hist.snapshot()[""]["count"] == 1


def test_registry_renders_json_and_prometheus() -> None:
    registry = MetricsRegistry()
    registry.counter("req_total").inc(3)
    registry.histogram("req_dur").record(0.5)
    rendered = registry.render_json()
    assert "counters" in rendered and "histograms" in rendered
    assert "req_total" in rendered["counters"]
    assert "req_dur" in rendered["histograms"]

    prometheus = registry.render_prometheus()
    assert "req_total" in prometheus
    assert "req_dur" in prometheus


def test_normalize_route_collapses_identifier_segments() -> None:
    assert (
        normalize_route("/api/v1/jobs/550e8400-e29b-41d4-a716-446655440000")
        == "/api/v1/jobs/{id}"
    )
    assert normalize_route("/api/v1/candidates/123") == "/api/v1/candidates/{id}"
    assert normalize_route("/health/ready") == "/health/ready"


def test_get_registry_is_singleton() -> None:
    assert get_registry() is get_registry()


def test_scan_for_leaks_detects_markers() -> None:
    assert "email" in scan_for_leaks("contact alice@example.com now")
    assert "phone" in scan_for_leaks("call 13800138000")
    assert "bearer_token" in scan_for_leaks("Authorization: Bearer abc123DEF456")
    assert "api_key" in scan_for_leaks("key sk-1234567890abc")


def test_scan_for_leaks_clean_after_redact() -> None:
    dirty = "email alice@example.com phone 13800138000"
    clean = redact_value(dirty)
    assert scan_for_leaks(clean) == []
    assert scan_for_leaks(clean) == []


def test_run_context_merged_into_json_log() -> None:
    formatter = JsonLogFormatter(service="test", environment="test")
    context = RunContext(
        run_id="run-1",
        run_type="application_run",
        node="human_review",
        attempt=2,
        error_code=None,
    )
    token = set_run_context(context)
    try:
        record = logging.LogRecord("test", logging.INFO, "f.py", 1, "hello", None, None)
        payload = json.loads(formatter.format(record))
    finally:
        reset_run_context(token)

    assert payload["run_id"] == "run-1"
    assert payload["run_type"] == "application_run"
    assert payload["node"] == "human_review"
    assert payload["attempt"] == 2
    assert "error_code" not in payload  # None fields are omitted
