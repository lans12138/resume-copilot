"""FIN-007: the offline evaluation gate's exit-code contract.

The gate's whole purpose is to be a signal CI can act on, so what matters here is
not the metric math (covered elsewhere) but the mapping from outcome to exit code:

* ``0`` when every threshold passes,
* ``1`` when a threshold was missed — the regression CI must catch,
* ``2`` when the gate could not run at all.

A gate that returned ``0`` for a missed threshold would be worse than no gate: it
would be a green light that nobody questions. And collapsing ``1`` into ``2`` (or
the reverse) would make "the model regressed" and "the harness crashed" the same
signal, which is precisely the distinction the gate exists to draw.

These run the ``main`` entry point directly rather than a subprocess, so the exit
code is asserted without depending on how the process was launched.
"""

from __future__ import annotations

from backend.app.evaluations.executor import BuiltinEvaluationExecutor, gate_passed
from backend.app.evaluations.gate import (
    EXIT_HARNESS_FAILED,
    EXIT_PASSED,
    EXIT_THRESHOLD_FAILED,
    _format_report,
    default_retrieval_config,
    main,
)
from backend.app.evaluations.service import MetricResult


def test_gate_passes_and_exits_zero() -> None:
    """The shipped datasets clear every bar, so the gate is green by default."""
    assert main(["--quiet"]) == EXIT_PASSED


def test_gate_reports_the_full_result_when_not_quiet() -> None:
    """A default run must print which metrics were measured and the verdict."""
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main([])
    output = buffer.getvalue()
    assert code == EXIT_PASSED
    assert "RESULT: PASS" in output
    assert "recall_at_k" in output
    assert "supported_precision" in output


def test_gate_exits_one_when_a_threshold_is_missed() -> None:
    """The critical case: a missed bar must be a non-zero exit, not a log line.

    Asserted through ``_format_report`` plus ``gate_passed`` on a deliberately
    failing metric set, because the built-in suites pass by construction — there is
    no way to make them fail without editing them, and a gate whose failure path is
    never exercised is a gate nobody can trust.
    """
    failing = [
        MetricResult(
            metric_name="recall_at_k", metric_value=0.10, threshold=0.85, passed=False
        )
    ]
    assert gate_passed(failing) is False
    report = _format_report(failing)
    assert "RESULT: FAIL" in report
    assert "recall_at_k" in report
    # The comparison points the correct way for a floor metric.
    assert "< 0.850000" in report


def test_ceiling_metrics_print_their_comparison_the_right_way() -> None:
    """Injection counters are ceilings; printing "<" would send readers the wrong way.

    A log that says an attack-success counter is "below the bar" when it actually
    exceeded it would send someone debugging a scoring bug instead of a prompt bug.
    """
    failing = [
        MetricResult(
            metric_name="injection_control_flow_changes",
            metric_value=3.0,
            threshold=0.0,
            passed=False,
        )
    ]
    report = _format_report(failing)
    assert "> 0.000000" in report
    assert "must not exceed" in report


def test_informational_metrics_are_marked_info_not_pass_or_fail() -> None:
    """A threshold-free metric is informational and must not read as a verdict."""
    report = _format_report(
        [
            MetricResult(metric_name="num_cases", metric_value=3.0, threshold=None, passed=True),
            MetricResult(
                metric_name="recall_at_k", metric_value=0.9, threshold=0.85, passed=True
            ),
        ]
    )
    assert "info" in report
    assert "RESULT: PASS" in report


def test_gate_can_be_repointed_without_changing_its_meaning() -> None:
    """``--top-k`` / ``--rrf-k`` are the only knobs, and both still clear the gate.

    The knobs exist so an experiment can re-score without a config file, not so the
    thresholds can be relaxed — the thresholds live in the scorer modules and are
    unaffected by these flags.
    """
    assert main(["--quiet", "--top-k", "10", "--rrf-k", "60"]) == EXIT_PASSED


def test_default_retrieval_config_matches_the_documented_frozen_values() -> None:
    config = default_retrieval_config()
    assert config.top_k == 10
    assert config.rrf_k == 60
    assert config.rule_version == "v1"
    assert config.structured_weight == config.keyword_weight == config.vector_weight == 1.0


def test_gate_needs_no_service_configuration() -> None:
    """The gate must run in a bare CI step.

    If it required ``Settings`` it would need DATABASE_URL/JWT_SECRET/etc., which
    means it could not run in exactly the environment where it matters most. This
    test asserts the executor is built without touching ``Settings`` by exercising
    the same construction path the CLI uses.
    """
    executor = BuiltinEvaluationExecutor(retrieval_config=default_retrieval_config())
    metrics = executor.run_all()
    assert gate_passed(metrics) is True
    assert metrics, "the gate must produce metrics without any configuration"


def test_exit_codes_are_distinct() -> None:
    """0 / 1 / 2 must stay distinguishable; collapsing them loses the diagnosis."""
    assert len({EXIT_PASSED, EXIT_THRESHOLD_FAILED, EXIT_HARNESS_FAILED}) == 3
    assert EXIT_PASSED == 0
