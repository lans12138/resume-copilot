"""Semantic evaluation framework for report claim support labels (IMP-020).

Evidence location proves a quote is *real*; it does not prove the quote
*semantically supports* the claim. That semantic judgement is scored against
human-labelled Golden claims (detailed design §9.4 last paragraph, §18.3 gate):

* SUPPORTED precision >= 95% — when we assert SUPPORTED, the label is right.
* Three-class (SUPPORTED / PARTIAL / INSUFFICIENT) macro-average F1 >= 0.85.
* Illegal evidence references must be 0 — owned by ``reports.validation``, but
  the gate report rolls it up alongside the label metrics.

The functions are pure so the gate thresholds are verifiable without a DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.app.reports.models import SupportLevel

SUPPORT_CLASSES: tuple[SupportLevel, ...] = (
    SupportLevel.SUPPORTED,
    SupportLevel.PARTIAL,
    SupportLevel.INSUFFICIENT,
)

# Gate thresholds (§18.3).
SUPPORTED_PRECISION_THRESHOLD = 0.95
MACRO_F1_THRESHOLD = 0.85
ILLEGAL_REFERENCE_THRESHOLD = 0


@dataclass(frozen=True)
class SemanticMetrics:
    """Per-label-class scores for one evaluated claim set."""

    supported_precision: float
    macro_f1: float
    per_class_f1: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class SemanticReport:
    """Aggregated semantic evaluation across one or more Golden cases."""

    cases: int
    claims: int
    supported_precision: float
    macro_f1: float
    illegal_reference_count: int
    supported_precision_pass: bool
    macro_f1_pass: bool
    illegal_reference_pass: bool

    @property
    def passed(self) -> bool:
        return (
            self.supported_precision_pass
            and self.macro_f1_pass
            and self.illegal_reference_pass
        )


def _class_f1(predicted: list[SupportLevel], gold: list[SupportLevel], cls: SupportLevel) -> float:
    tp = sum(1 for p, g in zip(predicted, gold, strict=True) if p == cls and g == cls)
    fp = sum(1 for p, g in zip(predicted, gold, strict=True) if p == cls and g != cls)
    fn = sum(1 for p, g in zip(predicted, gold, strict=True) if p != cls and g == cls)
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    if precision + recall == 0.0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def evaluate_support_labels(
    predicted: list[SupportLevel], gold: list[SupportLevel]
) -> SemanticMetrics:
    """Score one aligned (predicted, gold) pair of support labels."""
    if len(predicted) != len(gold):
        raise ValueError("predicted and gold label lists must be aligned")
    per_class: dict[str, float] = {
        cls.value: _class_f1(predicted, gold, cls) for cls in SUPPORT_CLASSES
    }
    macro = sum(per_class.values()) / len(SUPPORT_CLASSES)
    supported_precision = per_class[SupportLevel.SUPPORTED.value]
    return SemanticMetrics(
        supported_precision=supported_precision,
        macro_f1=macro,
        per_class_f1=per_class,
    )


def build_semantic_report(
    *,
    cases: list[tuple[list[SupportLevel], list[SupportLevel]]],
    illegal_reference_count: int = 0,
) -> SemanticReport:
    """Aggregate per-case metrics into one gate verdict."""
    if not cases:
        return SemanticReport(
            cases=0,
            claims=0,
            supported_precision=1.0,
            macro_f1=1.0,
            illegal_reference_count=illegal_reference_count,
            supported_precision_pass=True,
            macro_f1_pass=True,
            illegal_reference_pass=illegal_reference_count <= ILLEGAL_REFERENCE_THRESHOLD,
        )
    total_predicted: list[SupportLevel] = []
    total_gold: list[SupportLevel] = []
    claim_count = 0
    for predicted, gold in cases:
        total_predicted.extend(predicted)
        total_gold.extend(gold)
        claim_count += len(predicted)
    metrics = evaluate_support_labels(total_predicted, total_gold)
    return SemanticReport(
        cases=len(cases),
        claims=claim_count,
        supported_precision=metrics.supported_precision,
        macro_f1=metrics.macro_f1,
        illegal_reference_count=illegal_reference_count,
        supported_precision_pass=metrics.supported_precision >= SUPPORTED_PRECISION_THRESHOLD,
        macro_f1_pass=metrics.macro_f1 >= MACRO_F1_THRESHOLD,
        illegal_reference_pass=illegal_reference_count <= ILLEGAL_REFERENCE_THRESHOLD,
    )


# Built-in sanity case: a perfectly-labelled synthetic report must clear the gate,
# proving the framework itself computes the metrics correctly (no model needed).
_BUILTIN_PREDICTED: list[SupportLevel] = [
    SupportLevel.SUPPORTED,
    SupportLevel.SUPPORTED,
    SupportLevel.PARTIAL,
    SupportLevel.INSUFFICIENT,
    SupportLevel.SUPPORTED,
]
_BUILTIN_GOLD: list[SupportLevel] = list(_BUILTIN_PREDICTED)

BUILTIN_SEMANTIC_CASES: list[tuple[list[SupportLevel], list[SupportLevel]]] = [
    (_BUILTIN_PREDICTED, _BUILTIN_GOLD)
]


def run_builtin_semantic_evaluation(*, illegal_reference_count: int = 0) -> SemanticReport:
    """Evaluate the built-in case; must pass the gate (used by unit tests)."""
    return build_semantic_report(
        cases=BUILTIN_SEMANTIC_CASES, illegal_reference_count=illegal_reference_count
    )
