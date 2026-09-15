"""FIN-007: evaluation product closure — persistence, gate, duplicate guard.

The three scorers already existed and were already tested; what FIN-007 adds is the
part around them. So these tests deliberately do **not** re-verify the metric math.
They pin the properties a persisted verdict must have, each against the failure it
exists to prevent:

**The duplicate guard (409 EVALUATION_DUPLICATE).** Re-submitting the same
(dataset, config) while a run is in flight must be refused. Otherwise a
double-click, a retried request, or a CI job that fired twice would score the same
thing twice and produce two verdicts that ought to be identical — and nothing in
the system would notice if they were not. The guard is tested to be independent of
the caller, and to be *bypassable* only by an actually different config.

**A finished run is immutable.** §4.6 forbids editing a terminal run or its
metrics. ``test_complete_refuses_to_recompute_a_terminal_run`` and the
``replace_metrics`` guards exist because the alternative — a late or duplicated
worker overwriting a published verdict — is exactly the thing that makes a cited
CI signal untrustworthy.

**A missed threshold is not a harness failure.** A gate that fails is a
``COMPLETED`` run with ``passed=False``; only an unusable evaluation is ``FAILED``.
``test_complete_records_passed_false_as_completed`` and
``test_fail_marks_the_run_failed_not_completed`` pin that distinction, because
collapsing them would make "the model regressed" indistinguishable from "the
harness crashed" — the one thing the gate exists to tell apart.

**The gate is the conjunction of thresholded metrics only.** Adding an
informational metric must not be able to change the verdict, or a purely cosmetic
report change would silently alter whether CI goes red.

Where the assertion is about a SQL-enforced guard (the immutability predicate, the
metric uniqueness), the test inspects behaviour the in-memory adapter mirrors; the
handful of things only a real database can show are covered by the Postgres
integration suite.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.app.core.errors import AppError
from backend.app.evaluations.executor import (
    BuiltinEvaluationExecutor,
    failing_metrics,
    gate_passed,
)
from backend.app.evaluations.models import (
    EvaluationKind,
    EvaluationRun,
    EvaluationStatus,
    is_terminal,
)
from backend.app.evaluations.repository import (
    InMemoryDatasetVersionRepository,
    InMemoryEvaluationRunRepository,
)
from backend.app.evaluations.service import (
    EvaluationService,
    MetricResult,
    config_digest,
    content_digest,
)
from backend.app.retrieval.models import RetrievalConfig

FIXED_NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)


def _retrieval_config() -> RetrievalConfig:
    return RetrievalConfig(
        structured_weight=1.0,
        keyword_weight=1.0,
        vector_weight=1.0,
        rrf_k=60,
        top_k=10,
        rule_version="v1",
    )


class Harness:
    """In-memory wiring for the evaluation service."""

    def __init__(self) -> None:
        self.datasets = InMemoryDatasetVersionRepository()
        self.runs = InMemoryEvaluationRunRepository()
        self.now = FIXED_NOW
        self.service = EvaluationService(
            datasets=self.datasets, runs=self.runs, now=lambda: self.now
        )

    async def register_default_dataset(self) -> UUID:
        dataset = await self.service.register_dataset(
            name="retrieval-golden",
            version="v1",
            schema_version="1",
            manifest={"cases": 3},
            content={"cases": ["a", "b", "c"]},
        )
        return dataset.id

    async def create_run(
        self,
        dataset_version_id: UUID,
        *,
        kind: EvaluationKind = EvaluationKind.GOLDEN,
        config: dict[str, Any] | None = None,
        created_by: UUID | None = None,
    ) -> EvaluationRun:
        return await self.service.create_run(
            dataset_version_id=dataset_version_id,
            kind=kind,
            config=config if config is not None else {"top_k": 10},
            model_snapshot={"chat_model": "fake"},
            prompt_versions={"prompt_version": "v1"},
            created_by=created_by,
        )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# Dataset registration
# ----------------------------------------------------------------------


def test_register_dataset_is_idempotent_for_identical_content() -> None:
    """Re-registering the same version with the same content returns the same row.

    The unique constraint on ``(name, version)`` means a second insert would fail;
    the versioning contract says a repeated registration of *identical* content is
    a no-op rather than an error.
    """

    async def scenario() -> None:
        harness = Harness()
        first = await harness.register_default_dataset()
        second = await harness.register_default_dataset()
        assert first == second
        assert len(await harness.datasets.list_all()) == 1

    run(scenario())


def test_register_dataset_refuses_to_change_content_under_one_version() -> None:
    """A different payload under an existing version is 409, not a silent overwrite.

    Overwriting would invalidate every verdict already attributed to that version,
    and the attribution is the entire reason the data is versioned.
    """

    async def scenario() -> None:
        harness = Harness()
        await harness.register_default_dataset()
        with pytest.raises(AppError) as error:
            await harness.service.register_dataset(
                name="retrieval-golden",
                version="v1",
                schema_version="1",
                manifest={"cases": 4},
                content={"cases": ["a", "b", "c", "d"]},
            )
        assert error.value.code == "DATASET_VERSION_IMMUTABLE"
        assert error.value.http_status == 409

    run(scenario())


def test_content_digest_is_order_insensitive() -> None:
    """Canonicalisation means key order cannot change a dataset's identity."""
    assert content_digest({"a": 1, "b": 2}) == content_digest({"b": 2, "a": 1})


# ----------------------------------------------------------------------
# Duplicate guard (§12.6 409 EVALUATION_DUPLICATE)
# ----------------------------------------------------------------------


def test_duplicate_submission_is_rejected_while_a_run_is_in_flight() -> None:
    """The core of the 409: same dataset + same config while one is already live."""

    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        await harness.create_run(dataset_id)
        with pytest.raises(AppError) as error:
            await harness.create_run(dataset_id)
        assert error.value.code == "EVALUATION_DUPLICATE"
        assert error.value.http_status == 409

    run(scenario())


def test_duplicate_guard_applies_to_running_runs_not_only_created() -> None:
    """A claimed (RUNNING) run must also block a resubmission.

    If only ``CREATED`` counted, the guard would evaporate the instant a worker
    picked the run up — which is the whole window a duplicate would otherwise slip
    through.
    """

    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        first = await harness.create_run(dataset_id)
        await harness.service.start(first.id)
        with pytest.raises(AppError) as error:
            await harness.create_run(dataset_id)
        assert error.value.code == "EVALUATION_DUPLICATE"

    run(scenario())


def test_duplicate_guard_does_not_block_a_different_config() -> None:
    """A genuinely different config is a different experiment, not a duplicate.

    This is the negative control: a guard that blocked *every* second submission
    would pass the two tests above while making the feature useless.
    """

    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        await harness.create_run(dataset_id, config={"top_k": 10})
        second = await harness.create_run(dataset_id, config={"top_k": 20})
        assert second.status is EvaluationStatus.CREATED

    run(scenario())


def test_duplicate_guard_is_independent_of_the_caller() -> None:
    """A different actor cannot slip a duplicate past the guard.

    Keying on the actor would let two users (or a CI bot and a human) each start
    the same evaluation, producing two verdicts for one question.
    """

    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        await harness.create_run(dataset_id, created_by=uuid4())
        with pytest.raises(AppError) as error:
            await harness.create_run(dataset_id, created_by=uuid4())
        assert error.value.code == "EVALUATION_DUPLICATE"

    run(scenario())


def test_duplicate_guard_releases_once_the_run_is_terminal() -> None:
    """After a run finishes, the same (dataset, config) may be re-run deliberately.

    A re-run is a new row (§4.6), so the guard must consider only live statuses —
    otherwise a completed evaluation would permanently forbid re-scoring.
    """

    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        first = await harness.create_run(dataset_id)
        await harness.service.start(first.id)
        await harness.service.complete(first.id, [])
        second = await harness.create_run(dataset_id)
        assert second.id != first.id

    run(scenario())


def test_config_digest_ignores_key_order_but_not_values() -> None:
    """The hash is canonical, so reordering a dict is not a way to dodge the guard."""
    assert config_digest({"a": 1, "b": 2}) == config_digest({"b": 2, "a": 1})
    assert config_digest({"a": 1}) != config_digest({"a": 2})


def test_create_run_rejects_an_unknown_dataset_version() -> None:
    async def scenario() -> None:
        harness = Harness()
        with pytest.raises(AppError) as error:
            await harness.create_run(uuid4())
        assert error.value.code == "DATASET_VERSION_NOT_FOUND"
        assert error.value.http_status == 404

    run(scenario())


# ----------------------------------------------------------------------
# Claim and terminal transitions
# ----------------------------------------------------------------------


def test_start_claims_a_created_run_once() -> None:
    """The claim is the at-least-once guard: the second delivery is a no-op."""

    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        created = await harness.create_run(dataset_id)

        claimed = await harness.service.start(created.id)
        assert claimed is not None
        assert claimed.status is EvaluationStatus.RUNNING
        assert claimed.started_at == FIXED_NOW

        # A duplicate delivery finds the run already claimed.
        assert await harness.service.start(created.id) is None

    run(scenario())


def test_start_returns_none_for_a_missing_run() -> None:
    async def scenario() -> None:
        harness = Harness()
        assert await harness.service.start(uuid4()) is None

    run(scenario())


def test_complete_records_metrics_and_a_passing_verdict() -> None:
    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        created = await harness.create_run(dataset_id)
        await harness.service.start(created.id)

        finished = await harness.service.complete(
            created.id,
            [
                MetricResult(
                    metric_name="recall_at_k",
                    metric_value=0.91,
                    threshold=0.85,
                    passed=True,
                )
            ],
        )
        assert finished is not None
        assert finished.status is EvaluationStatus.COMPLETED
        assert finished.passed is True
        assert finished.finished_at == FIXED_NOW
        metrics = await harness.service.list_metrics(created.id)
        assert [m.metric_name for m in metrics] == ["recall_at_k"]

    run(scenario())


def test_complete_records_passed_false_as_completed_not_failed() -> None:
    """A missed threshold is a successful run with a negative verdict.

    This is the distinction the gate exists to surface: "the numbers missed the
    bar" must not look like "the harness broke".
    """

    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        created = await harness.create_run(dataset_id)
        await harness.service.start(created.id)

        finished = await harness.service.complete(
            created.id,
            [
                MetricResult(
                    metric_name="recall_at_k",
                    metric_value=0.40,
                    threshold=0.85,
                    passed=False,
                )
            ],
        )
        assert finished is not None
        assert finished.status is EvaluationStatus.COMPLETED
        assert finished.passed is False
        assert finished.error_code is None

    run(scenario())


def test_fail_marks_the_run_failed_not_completed() -> None:
    """A broken harness is FAILED with a safe code, never COMPLETED."""

    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        created = await harness.create_run(dataset_id)
        await harness.service.start(created.id)

        failed = await harness.service.fail(
            created.id,
            error_code="EVALUATION_EXECUTION_FAILED",
            safe_message="评测执行失败，请查看服务日志",
        )
        assert failed is not None
        assert failed.status is EvaluationStatus.FAILED
        assert failed.error_code == "EVALUATION_EXECUTION_FAILED"
        # A failed run has no verdict; ``passed`` stays unset rather than False.
        assert failed.passed is None

    run(scenario())


def test_complete_refuses_to_recompute_a_terminal_run() -> None:
    """§4.6 immutability: a late worker cannot overwrite a published verdict."""

    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        created = await harness.create_run(dataset_id)
        await harness.service.start(created.id)
        first = await harness.service.complete(
            created.id,
            [
                MetricResult(
                    metric_name="recall_at_k", metric_value=0.91, threshold=0.85, passed=True
                )
            ],
        )
        assert first is not None and first.passed is True

        # A second, contradictory completion must be refused outright.
        assert (
            await harness.service.complete(
                created.id,
                [
                    MetricResult(
                        metric_name="recall_at_k",
                        metric_value=0.10,
                        threshold=0.85,
                        passed=False,
                    )
                ],
            )
            is None
        )
        stored = await harness.service.list_metrics(created.id)
        assert [m.metric_value for m in stored] == [0.91]

    run(scenario())


def test_fail_refuses_to_touch_a_terminal_run() -> None:
    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        created = await harness.create_run(dataset_id)
        await harness.service.start(created.id)
        await harness.service.complete(created.id, [])
        assert (
            await harness.service.fail(created.id, error_code="X", safe_message="y") is None
        )

    run(scenario())


def test_replace_metrics_is_a_noop_on_a_terminal_run() -> None:
    """The repository-level guard, independent of the service-level one."""

    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        created = await harness.create_run(dataset_id)
        await harness.service.start(created.id)
        await harness.service.complete(
            created.id,
            [
                MetricResult(
                    metric_name="mrr", metric_value=0.80, threshold=0.70, passed=True
                )
            ],
        )
        from backend.app.evaluations.repository import metric_snapshot_rows

        rows = metric_snapshot_rows(
            created.id,
            [{"metric_name": "mrr", "metric_value": 0.01, "threshold": 0.70, "passed": False}],
        )
        await harness.runs.replace_metrics(created.id, rows)
        stored = await harness.service.list_metrics(created.id)
        assert [m.metric_value for m in stored] == [0.80]

    run(scenario())


def test_replace_metrics_supersedes_rather_than_appends() -> None:
    """Second pass over one suite replaces its numbers; it does not collide.

    ``uq_metric_snapshots_run_metric`` makes an append a hard error, and the
    correct semantic for "compute this run's numbers" is wholesale replacement.
    """

    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        created = await harness.create_run(dataset_id)
        await harness.service.start(created.id)
        await harness.service.record_metrics(
            created.id,
            [MetricResult(metric_name="mrr", metric_value=0.50, threshold=0.70, passed=False)],
        )
        await harness.service.record_metrics(
            created.id,
            [MetricResult(metric_name="mrr", metric_value=0.75, threshold=0.70, passed=True)],
        )
        stored = await harness.service.list_metrics(created.id)
        assert len(stored) == 1
        assert stored[0].metric_value == 0.75

    run(scenario())


def test_is_terminal_covers_exactly_the_finished_states() -> None:
    assert is_terminal(EvaluationStatus.COMPLETED)
    assert is_terminal(EvaluationStatus.FAILED)
    assert not is_terminal(EvaluationStatus.CREATED)
    assert not is_terminal(EvaluationStatus.RUNNING)


# ----------------------------------------------------------------------
# Verdict computation
# ----------------------------------------------------------------------


def test_complete_is_the_conjunction_of_thresholded_metrics_only() -> None:
    """Informational metrics cannot change the verdict.

    If they could, adding a diagnostic row would silently alter whether CI goes
    red — a coupling nobody would expect when editing a report.
    """

    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        created = await harness.create_run(dataset_id)
        await harness.service.start(created.id)
        finished = await harness.service.complete(
            created.id,
            [
                MetricResult(
                    metric_name="recall_at_k", metric_value=0.90, threshold=0.85, passed=True
                ),
                # No threshold: informational, and marked failed to prove it is ignored.
                MetricResult(
                    metric_name="num_cases", metric_value=3.0, threshold=None, passed=False
                ),
            ],
        )
        assert finished is not None
        assert finished.passed is True

    run(scenario())


def test_one_failing_threshold_fails_the_run() -> None:
    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        created = await harness.create_run(dataset_id)
        await harness.service.start(created.id)
        finished = await harness.service.complete(
            created.id,
            [
                MetricResult(
                    metric_name="recall_at_k", metric_value=0.90, threshold=0.85, passed=True
                ),
                MetricResult(
                    metric_name="ndcg_at_k", metric_value=0.50, threshold=0.80, passed=False
                ),
            ],
        )
        assert finished is not None
        assert finished.passed is False

    run(scenario())


def test_complete_without_metrics_fails_the_gate() -> None:
    """Vacuous truth must not pass the gate.

    ``all()`` over an empty list is True, so a run that produced no metrics at all
    would otherwise be reported as passing. That would make "the scorer silently
    returned nothing" look like a green gate.
    """

    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        created = await harness.create_run(dataset_id)
        await harness.service.start(created.id)
        finished = await harness.service.complete(created.id, [])
        assert finished is not None
        assert finished.passed is False
        # The gate helper used by CI must agree with the persisted verdict.
        assert gate_passed([]) is False

    run(scenario())


# ----------------------------------------------------------------------
# Pagination and reads
# ----------------------------------------------------------------------


def test_list_page_orders_newest_first_and_counts_the_total() -> None:
    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        first = await harness.create_run(dataset_id, config={"n": 1})
        harness.now = FIXED_NOW + timedelta(minutes=1)
        second = await harness.create_run(dataset_id, config={"n": 2})

        page = await harness.service.list_page(page=1, page_size=10)
        assert page.total == 2
        assert [r.id for r in page.items] == [second.id, first.id]

    run(scenario())


def test_list_page_filters_by_status_and_kind() -> None:
    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        golden = await harness.create_run(dataset_id, kind=EvaluationKind.GOLDEN)
        harness.now = FIXED_NOW + timedelta(minutes=1)
        await harness.create_run(
            dataset_id, kind=EvaluationKind.INJECTION, config={"other": True}
        )

        by_kind = await harness.service.list_page(kind=EvaluationKind.GOLDEN)
        assert [r.id for r in by_kind.items] == [golden.id]

        await harness.service.start(golden.id)
        by_status = await harness.service.list_page(status=EvaluationStatus.RUNNING)
        assert [r.id for r in by_status.items] == [golden.id]

    run(scenario())


def test_list_page_clamps_an_oversized_page_size() -> None:
    """The page cap keeps one request from pulling the entire history."""

    async def scenario() -> None:
        harness = Harness()
        dataset_id = await harness.register_default_dataset()
        await harness.create_run(dataset_id)
        page = await harness.service.list_page(page=0, page_size=10_000)
        assert page.limit == 100
        assert page.offset == 0

    run(scenario())


def test_get_run_raises_not_found() -> None:
    async def scenario() -> None:
        harness = Harness()
        with pytest.raises(AppError) as error:
            await harness.service.get_run(uuid4())
        assert error.value.code == "EVALUATION_NOT_FOUND"
        assert error.value.http_status == 404

    run(scenario())


# ----------------------------------------------------------------------
# Built-in executor: the gate itself
# ----------------------------------------------------------------------


def test_builtin_executor_passes_every_gate() -> None:
    """The shipped datasets must clear all three suites' thresholds."""

    def scenario() -> None:
        executor = BuiltinEvaluationExecutor(retrieval_config=_retrieval_config())
        for kind in (
            EvaluationKind.GOLDEN,
            EvaluationKind.SEMANTIC,
            EvaluationKind.INJECTION,
        ):
            metrics = executor.run(kind)
            assert metrics, f"{kind} produced no metrics"
            assert failing_metrics(metrics) == [], f"{kind} failed: {failing_metrics(metrics)}"
            assert gate_passed(metrics) is True

    scenario()


def test_builtin_executor_reports_the_documented_metric_names() -> None:
    """The metric names are a contract with the report and the CI gate."""
    executor = BuiltinEvaluationExecutor(retrieval_config=_retrieval_config())
    names = {metric.metric_name for metric in executor.run_all()}
    assert "recall_at_k" in names
    assert "supported_precision" in names
    assert "support_macro_f1" in names
    assert "illegal_reference_count" in names
    # All six injection counters are reported separately, not summed into one.
    for counter in (
        "control_flow_changes",
        "unauthorized_tool_proposals",
        "approval_bypasses",
        "attack_target_hits",
        "paired_hard_rule_changes",
        "new_unsupported_high_impact_claims",
    ):
        assert f"injection_{counter}" in names


def test_every_thresholded_metric_records_the_bar_it_was_measured_against() -> None:
    """A thresholded metric must carry its bar, or the verdict is unauditable."""
    executor = BuiltinEvaluationExecutor(retrieval_config=_retrieval_config())
    thresholded = [m for m in executor.run_all() if m.threshold is not None]
    assert thresholded, "the built-in suites must produce thresholded metrics"
    for metric in thresholded:
        # Minimum-style metrics (counts that must stay at/below a bar) are the
        # exception to the "higher is better" direction, so each metric is checked
        # against its own predicate rather than a blanket comparison.
        threshold = metric.threshold
        assert threshold is not None, "filtered to thresholded metrics above"
        if metric.metric_name == "illegal_reference_count" or metric.metric_name.startswith(
            "injection_"
        ):
            assert metric.passed == (metric.metric_value <= threshold)
        else:
            assert metric.passed == (metric.metric_value >= threshold)


def test_gate_passed_ignores_informational_metrics() -> None:
    metrics = [
        MetricResult(metric_name="a", metric_value=1.0, threshold=0.5, passed=True),
        MetricResult(metric_name="b", metric_value=0.0, threshold=None, passed=False),
    ]
    assert gate_passed(metrics) is True


def test_unknown_kind_yields_no_metrics_rather_than_guessing() -> None:
    executor = BuiltinEvaluationExecutor(retrieval_config=_retrieval_config())
    assert executor.run("NOT_A_KIND") == []  # type: ignore[arg-type]
