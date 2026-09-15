"""Process-local metrics with JSON and Prometheus exposition (IMP-029, §18.2).

Single-process metrics collection for the MVP. A multi-worker deployment would
swap this for a shared exporter (e.g. the Prometheus client), but the public
surface (Counter / Histogram / Timer / MetricsRegistry.render_*) is intentionally
stable so call sites do not change.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# MVP bounds retained samples per labelled series to keep memory predictable.
_MAX_SAMPLES_PER_SERIES = 10_000

_ROUTE_TOKEN = re.compile(
    r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|/[0-9a-fA-F]{8,}"
    r"|/\d+(?=/|$)"
)


def normalize_route(path: str) -> str:
    """Collapse identifier path segments to one template token for stable labels."""
    return _ROUTE_TOKEN.sub("/{id}", path)


def _labels_to_str(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    parts = [f'{key}="{value}"' for key, value in labels.items()]
    return "{" + ",".join(parts) + "}"


def _quantile(sorted_values: list[float], quantile: float) -> float | None:
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    index = quantile * (n - 1)
    low = int(index)
    high = min(low + 1, n - 1)
    fraction = index - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * fraction


def _summarize(values: list[float]) -> dict[str, float | None]:
    n = len(values)
    if n == 0:
        return {
            "count": 0,
            "sum": 0.0,
            "mean": 0.0,
            "min": 0.0,
            "max": 0.0,
            "p50": None,
            "p95": None,
            "p99": None,
        }
    ordered = sorted(values)
    total = sum(ordered)
    return {
        "count": n,
        "sum": total,
        "mean": total / n,
        "min": ordered[0],
        "max": ordered[-1],
        "p50": _quantile(ordered, 0.5),
        "p95": _quantile(ordered, 0.95),
        "p99": _quantile(ordered, 0.99),
    }


class Counter:
    """Monotonic counter with optional label dimensions."""

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description
        self._values: dict[tuple[tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def snapshot(self) -> dict[str, float]:
        return {_labels_to_str(dict(key)): value for key, value in self._values.items()}


class Histogram:
    """Sample distribution with count/sum/mean/min/max/p50/p95/p99."""

    def __init__(
        self,
        name: str,
        description: str = "",
        max_samples: int = _MAX_SAMPLES_PER_SERIES,
    ) -> None:
        self.name = name
        self.description = description
        self._series: dict[tuple[tuple[str, str], ...], deque[float]] = {}
        self._max = max_samples
        self._lock = threading.Lock()

    def record(self, value: float, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            series = self._series.get(key)
            if series is None:
                series = deque(maxlen=self._max)
                self._series[key] = series
            series.append(float(value))

    def snapshot(self) -> dict[str, dict[str, float | None]]:
        with self._lock:
            return {
                _labels_to_str(dict(key)): _summarize(list(series))
                for key, series in self._series.items()
            }


class Timer:
    """Context manager that records elapsed seconds into a Histogram on exit."""

    def __init__(self, histogram: Histogram, **labels: str) -> None:
        self._histogram = histogram
        self._labels = labels
        self._start = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_exc: object) -> None:
        elapsed = time.perf_counter() - self._start
        self._histogram.record(elapsed, **self._labels)


class MetricsRegistry:
    """Process-local registry of counters and histograms."""

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, description: str = "") -> Counter:
        with self._lock:
            existing = self._counters.get(name)
            if existing is None:
                existing = Counter(name, description)
                self._counters[name] = existing
            return existing

    def histogram(self, name: str, description: str = "") -> Histogram:
        with self._lock:
            existing = self._histograms.get(name)
            if existing is None:
                existing = Histogram(name, description)
                self._histograms[name] = existing
            return existing

    def render_json(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": {name: counter.snapshot() for name, counter in self._counters.items()},
                "histograms": {name: hist.snapshot() for name, hist in self._histograms.items()},
            }

    def render_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name, counter in self._counters.items():
                lines.append(f"# HELP {name} {counter.description}".rstrip())
                lines.append(f"# TYPE {name} counter")
                for label_str, value in counter.snapshot().items():
                    lines.append(f"{name}{label_str} {value}")
            for name, hist in self._histograms.items():
                lines.append(f"# HELP {name} {hist.description}".rstrip())
                lines.append(f"# TYPE {name} histogram")
                for label_str, stats in hist.snapshot().items():
                    if stats["count"] == 0:
                        continue
                    lines.append(f"{name}_count{label_str} {stats['count']}")
                    lines.append(f"{name}_sum{label_str} {stats['sum']}")
                    for quantile, key in (("0.5", "p50"), ("0.95", "p95"), ("0.99", "p99")):
                        if label_str:
                            qlabels = "{" + f'quantile="{quantile}"' + "," + label_str[1:]
                        else:
                            qlabels = f'{{quantile="{quantile}"}}'
                        lines.append(f"{name}{qlabels} {stats[key]}")
        return "\n".join(lines) + "\n"


_REGISTRY: MetricsRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_registry() -> MetricsRegistry:
    """Return the process-wide metrics registry (created on first use)."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = MetricsRegistry()
        return _REGISTRY


# Pre-defined API-layer metrics from detailed design §18.2.
def http_requests_total() -> Counter:
    return get_registry().counter(
        "http_requests_total",
        "Total HTTP requests by route, method and status code.",
    )


def http_request_duration_seconds() -> Histogram:
    return get_registry().histogram(
        "http_request_duration_seconds",
        "HTTP request duration in seconds by route and method.",
    )


def http_auth_failures_total() -> Counter:
    return get_registry().counter(
        "http_auth_failures_total",
        "HTTP 401/403 responses by method (authorization failures).",
    )


def http_conflict_total() -> Counter:
    return get_registry().counter(
        "http_conflict_total",
        "HTTP 409 conflict responses by method.",
    )


@contextmanager
def timed(label: str) -> Iterator[Timer]:
    """Convenience context manager that records into http_request_duration_seconds."""
    timer = Timer(http_request_duration_seconds(), route=label)
    with timer:
        yield timer
