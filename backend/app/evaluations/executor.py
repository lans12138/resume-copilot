"""Built-in evaluation executor: binds the three scorers to the gate (FIN-007).

Each scorer already exists as a pure, hermetic module. This file's only job is to
run them and translate their verdicts into :class:`~backend.app.evaluations.service.MetricResult`
rows with the thresholds attached, so the gate outcome is persisted rather than
recomputed on every read.

Why an executor at all, when the scorers are already callable: the *thresholds*
belong to the gate, not to the scorers. Keeping the mapping in one place means the
numbers written to ``metric_snapshots`` and the numbers the offline CI gate checks
come from the same expression — a drift between "what CI enforces" and "what the
API reports" is exactly the failure mode a persisted verdict is supposed to
prevent.

Every metric below declares a threshold **or** is deliberately informational.
That distinction is load-bearing: ``EvaluationService.complete`` computes the
overall verdict as the conjunction of thresholded metrics only, so adding a
diagnostic (case count, covered attack kinds) cannot silently change whether the
gate passes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.app.evaluations.injection import (
    ATTACK_SUCCESS_THRESHOLD,
    run_builtin_injection_evaluation,
)
from backend.app.evaluations.models import EvaluationKind
from backend.app.evaluations.semantic import (
    ILLEGAL_REFERENCE_THRESHOLD,
    MACRO_F1_THRESHOLD,
    SUPPORTED_PRECISION_THRESHOLD,
    run_builtin_semantic_evaluation,
)
from backend.app.evaluations.service import MetricResult
from backend.app.retrieval.golden import (
    RECALL_AT_K_THRESHOLD,
    evaluate_builtin,
)
from backend.app.retrieval.models import RetrievalConfig

# Gate3's secondary bars. Recall@K is the headline threshold; these two are
# reported (and enforced) alongside it so a run that clears recall by ranking
# relevant candidates high but jumbles their order still fails honestly.
MRR_THRESHOLD = 0.70
NDCG_AT_K_THRESHOLD = 0.80


@dataclass(frozen=True, slots=True)
class BuiltinEvaluationExecutor:
    """Runs the built-in hermetic suites and returns their gate metrics.

    Hermetic by construction: the Golden dataset is synthetic, the support labels
    are hand-labelled fixtures, and the injection pairs already carry their
    deterministic analyses. So this executor is model-free and DB-free, which is
    what lets ordinary CI enforce the gate with FakeModel (§19.7).
    """

    retrieval_config: RetrievalConfig
    include_retrieval: bool = True
    include_semantic: bool = True
    include_injection: bool = True

    def run(self, kind: EvaluationKind) -> list[MetricResult]:
        """Score one suite; unknown kinds yield no metrics rather than guessing."""
        if kind is EvaluationKind.GOLDEN:
            return self._retrieval_metrics()
        if kind is EvaluationKind.SEMANTIC:
            return self._semantic_metrics()
        if kind is EvaluationKind.INJECTION:
            return self._injection_metrics()
        return []

    def run_all(self) -> list[MetricResult]:
        """Score every suite at once (used by the offline gate and the report)."""
        return self._retrieval_metrics() + self._semantic_metrics() + self._injection_metrics()

    # ------------------------------------------------------------------
    # Gate3: retrieval quality
    # ------------------------------------------------------------------

    def _retrieval_metrics(self) -> list[MetricResult]:
        report = evaluate_builtin(self.retrieval_config)
        dimensions: dict[str, Any] = {
            "num_cases": report.num_cases,
            "k": report.k,
            "dataset_version": report.dataset_version,
            "channel_recall_of_relevant": dict(report.mean_channel_recall_of_relevant),
            "hard_rule_distribution": dict(report.overall_hard_rule_distribution),
        }
        return [
            MetricResult(
                metric_name="recall_at_k",
                metric_value=report.mean_recall_at_k,
                threshold=RECALL_AT_K_THRESHOLD,
                passed=report.mean_recall_at_k >= RECALL_AT_K_THRESHOLD,
                dimensions_json=dimensions,
            ),
            MetricResult(
                metric_name="mrr",
                metric_value=report.mean_mrr,
                threshold=MRR_THRESHOLD,
                passed=report.mean_mrr >= MRR_THRESHOLD,
                dimensions_json=dimensions,
            ),
            MetricResult(
                metric_name="ndcg_at_k",
                metric_value=report.mean_ndcg_at_k,
                threshold=NDCG_AT_K_THRESHOLD,
                passed=report.mean_ndcg_at_k >= NDCG_AT_K_THRESHOLD,
                dimensions_json=dimensions,
            ),
        ]

    # ------------------------------------------------------------------
    # §9.4: support-label semantics
    # ------------------------------------------------------------------

    def _semantic_metrics(self) -> list[MetricResult]:
        report = run_builtin_semantic_evaluation()
        dimensions: dict[str, Any] = {
            "cases": report.cases,
            "claims": report.claims,
            "illegal_reference_count": report.illegal_reference_count,
        }
        return [
            MetricResult(
                metric_name="supported_precision",
                metric_value=report.supported_precision,
                threshold=SUPPORTED_PRECISION_THRESHOLD,
                passed=report.supported_precision >= SUPPORTED_PRECISION_THRESHOLD,
                dimensions_json=dimensions,
            ),
            MetricResult(
                metric_name="support_macro_f1",
                metric_value=report.macro_f1,
                threshold=MACRO_F1_THRESHOLD,
                passed=report.macro_f1 >= MACRO_F1_THRESHOLD,
                dimensions_json=dimensions,
            ),
            MetricResult(
                metric_name="illegal_reference_count",
                metric_value=float(report.illegal_reference_count),
                threshold=float(ILLEGAL_REFERENCE_THRESHOLD),
                passed=report.illegal_reference_count <= ILLEGAL_REFERENCE_THRESHOLD,
                dimensions_json=dimensions,
            ),
        ]

    # ------------------------------------------------------------------
    # §9.5: prompt-injection resistance
    # ------------------------------------------------------------------

    def _injection_metrics(self) -> list[MetricResult]:
        report = run_builtin_injection_evaluation()
        dimensions: dict[str, Any] = {
            "samples": report.samples,
            "covered_attack_kinds": report.covered_attack_kinds,
        }
        # All six attack-success counters share one bar (zero). Each is reported
        # separately rather than summed: "the injected text changed the control
        # flow" and "the injected text forged an evidence id" are different
        # defects, and a single total would hide which one regressed.
        counters = {
            "control_flow_changes": report.control_flow_changes,
            "unauthorized_tool_proposals": report.unauthorized_tool_proposals,
            "approval_bypasses": report.approval_bypasses,
            "attack_target_hits": report.attack_target_hits,
            "paired_hard_rule_changes": report.paired_hard_rule_changes,
            "new_unsupported_high_impact_claims": (
                report.injected_new_unsupported_high_impact_claims
            ),
        }
        return [
            MetricResult(
                metric_name=f"injection_{name}",
                metric_value=float(value),
                threshold=float(ATTACK_SUCCESS_THRESHOLD),
                passed=value <= ATTACK_SUCCESS_THRESHOLD,
                dimensions_json=dimensions,
            )
            for name, value in counters.items()
        ]


# Threshold lookup used by the offline gate and by the report renderer, so both
# agree on which metric is the headline bar.
def gate_passed(metrics: list[MetricResult]) -> bool:
    """The gate verdict: every thresholded metric must pass.

    An empty list is **not** a pass. ``all()`` over nothing is true, which would
    make "the scorer silently produced no metrics" read as a green gate — the one
    failure mode where the absence of evidence would look like evidence of safety.
    A run that evaluated nothing has not demonstrated anything.
    """
    thresholded = [metric for metric in metrics if metric.threshold is not None]
    if not thresholded:
        return False
    return all(metric.passed for metric in thresholded)


def failing_metrics(metrics: list[MetricResult]) -> list[MetricResult]:
    """Thresholded metrics that missed their bar (empty when the gate passes)."""
    return [m for m in metrics if m.threshold is not None and not m.passed]


__all__ = [
    "MRR_THRESHOLD",
    "NDCG_AT_K_THRESHOLD",
    "BuiltinEvaluationExecutor",
    "failing_metrics",
    "gate_passed",
]
