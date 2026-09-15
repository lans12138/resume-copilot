"""Offline evaluation gate entry point (FIN-007, §18.3 / §20.4).

Run as ``python -m backend.evaluations.gate``. Prints the gate report and exits
**non-zero when any threshold is missed**, which is what lets ordinary CI enforce
the gate without a model, a database, or a broker:

* ``exit 0`` — every thresholded metric passed.
* ``exit 1`` — at least one threshold was missed (the regressed case CI must catch).
* ``exit 2`` — the gate could not be evaluated at all (a scorer raised). This is
  deliberately distinct from ``1``: "the numbers missed the bar" and "the harness
  broke" need different responses, and a single failure code would blur them.

Why a CLI and not just a pytest assertion: the same numbers are written to
``metric_snapshots`` by ``evaluations.execute`` and rendered by the results page.
Having one executable expression of the gate means CI, the API, and the UI cannot
silently disagree about what "passing" means — a drift that would otherwise only
surface as a green CI run next to a red dashboard.

**Configuration independence.** The gate deliberately does *not* read ``Settings``.
It never touches a database, a broker, or a model, so requiring ``DATABASE_URL``,
``JWT_SECRET`` and friends would make the gate unrunnable in exactly the
environment where it matters most — a bare CI step that has no service
configuration. Retrieval knobs therefore come from the explicit defaults below,
matching the design's frozen evaluation configuration, and can be overridden by
flag for an experiment without mutating the gate's meaning.

The suites are hermetic by construction (synthetic Golden dataset, hand-labelled
support fixtures, deterministic injection pairs), so this runs identically under
FakeModel and against recorded responses (§19.7).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from backend.app.evaluations.executor import (
    BuiltinEvaluationExecutor,
    failing_metrics,
    gate_passed,
)
from backend.app.evaluations.service import MetricResult
from backend.app.retrieval.models import RetrievalConfig

EXIT_PASSED = 0
EXIT_THRESHOLD_FAILED = 1
EXIT_HARNESS_FAILED = 2

# The design's frozen retrieval configuration (§18.3). Kept here rather than read
# from ``Settings`` so the gate runs in a configuration-free environment; see the
# module docstring.
DEFAULT_TOP_K = 10
DEFAULT_RRF_K = 60


def default_retrieval_config(
    *, top_k: int = DEFAULT_TOP_K, rrf_k: int = DEFAULT_RRF_K
) -> RetrievalConfig:
    """The evaluation configuration the gate scores against."""
    return RetrievalConfig(
        structured_weight=1.0,
        keyword_weight=1.0,
        vector_weight=1.0,
        rrf_k=rrf_k,
        top_k=top_k,
        rule_version="v1",
    )


# Metrics whose bar is a ceiling ("must stay at or below") rather than a floor.
# Named explicitly because the direction is part of the metric's meaning, not
# something derivable from its value — and printing the wrong comparison in a CI
# log sends the reader looking for the wrong bug.
_CEILING_METRICS = frozenset({"illegal_reference_count"})


def _is_ceiling(metric_name: str) -> bool:
    """True when lower is better for this metric (counts that must stay at zero)."""
    return metric_name in _CEILING_METRICS or metric_name.startswith("injection_")


def _format_failure(metric: MetricResult) -> str:
    """One failure line, with the comparison pointing the right way."""
    assert metric.threshold is not None  # guaranteed by failing_metrics
    if _is_ceiling(metric.metric_name):
        return (
            f"  - {metric.metric_name}: {metric.metric_value:.6f} "
            f"> {metric.threshold:.6f} (must not exceed)"
        )
    return (
        f"  - {metric.metric_name}: {metric.metric_value:.6f} "
        f"< {metric.threshold:.6f} (below the bar)"
    )


def _format_report(metrics: list[MetricResult]) -> str:
    """One aligned line per metric, failures marked, so a CI log is readable."""
    lines = ["", "evaluation gate", "=" * 68]
    width = max((len(metric.metric_name) for metric in metrics), default=0)
    for metric in metrics:
        if metric.threshold is None:
            bar = "       -"
            verdict = "info"
        else:
            bar = f"{metric.threshold:8.4f}"
            verdict = "PASS" if metric.passed else "FAIL"
        lines.append(
            f"  {metric.metric_name.ljust(width)}  value={metric.metric_value:9.6f}  "
            f"threshold={bar}  {verdict}"
        )
    lines.append("=" * 68)
    failures = failing_metrics(metrics)
    if failures:
        lines.append(f"RESULT: FAIL ({len(failures)} threshold(s) missed)")
        lines.extend(_format_failure(metric) for metric in failures)
    else:
        lines.append("RESULT: PASS")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the gate and return the process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m backend.evaluations.gate",
        description="Run the offline evaluation gate; non-zero exit on a missed threshold.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only the exit-relevant summary.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"Retrieval Top-K to score against (default {DEFAULT_TOP_K}).",
    )
    parser.add_argument(
        "--rrf-k",
        type=int,
        default=DEFAULT_RRF_K,
        help=f"RRF constant to score against (default {DEFAULT_RRF_K}).",
    )
    args = parser.parse_args(argv)

    executor = BuiltinEvaluationExecutor(
        retrieval_config=default_retrieval_config(top_k=args.top_k, rrf_k=args.rrf_k)
    )
    try:
        metrics = executor.run_all()
    except Exception as error:  # noqa: BLE001 - harness boundary: distinct exit code
        print(f"evaluation gate could not run: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_HARNESS_FAILED

    if not metrics:
        # A scorer that returns nothing has demonstrated nothing; treating that as a
        # pass would turn a broken harness into a green gate.
        print("evaluation gate produced no metrics", file=sys.stderr)
        return EXIT_HARNESS_FAILED

    if not args.quiet:
        print(_format_report(metrics))
    passed = gate_passed(metrics)
    if not passed:
        print("EVALUATION_GATE_FAILED", file=sys.stderr)
        return EXIT_THRESHOLD_FAILED
    if args.quiet:
        print("EVALUATION_GATE_OK")
    return EXIT_PASSED


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(main())
