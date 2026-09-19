"""The call budget, and the two ways a decorator could flatter a run (PORT-004).

A live evaluation spends money per call, so the budget is a stated parameter of the
run. Two things are pinned here beyond the counting itself, and both are about a
wrapper not changing what the run *is*:

* a budgeted fake must not be reported as a live call, and
* a budgeted recording must not lose its incomplete-fixture check.

Either mistake would make the report claim more than the run did.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from backend.app.candidates.schemas import CandidateProfileDraft
from backend.app.documents.parsers import ParsedBlock
from backend.app.evaluations.budget import BudgetedGateway
from backend.app.evaluations.corpus import BUILTIN_CORPUS, Split
from backend.app.evaluations.runner import (
    BUDGET_EXCEEDED,
    INCOMPLETE_RECORDING,
    BudgetExceeded,
    EvaluationSetupError,
    PredictionSource,
    classify_gateway,
    gateway_usage,
    run_corpus,
    underlying_gateway,
)
from backend.app.infrastructure.chat_completion import UsageRecord
from backend.app.infrastructure.embedding import FakeEmbeddingGateway
from backend.app.infrastructure.model_gateway import FakeModelGateway
from backend.app.infrastructure.recording import RecordedModelGateway, Recording
from backend.app.retrieval.models import RetrievalConfig

_CONFIG = RetrievalConfig(
    structured_weight=1.0,
    keyword_weight=1.0,
    vector_weight=1.0,
    rrf_k=60,
    top_k=5,
    rule_version="eval-r1",
)


class _CountingGateway:
    """A gateway that reports how many times it was called."""

    version = "counting-v1"

    def __init__(self) -> None:
        self.calls = 0
        self.usage = UsageRecord()
        self.usage.record({"usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    async def extract_profile(
        self, *, full_text: str, blocks: Sequence[ParsedBlock]
    ) -> CandidateProfileDraft:
        self.calls += 1
        return CandidateProfileDraft()


def _call(gateway: BudgetedGateway) -> CandidateProfileDraft:
    return asyncio.run(gateway.extract_profile(full_text="x", blocks=[]))


def test_the_budget_stops_the_run_at_the_limit() -> None:
    inner = _CountingGateway()
    gateway = BudgetedGateway(inner, max_calls=2)
    assert _call(gateway).full_name is None
    assert _call(gateway).full_name is None
    with pytest.raises(BudgetExceeded) as error:
        _call(gateway)
    assert error.value.limit == 2
    assert error.value.code == BUDGET_EXCEEDED
    assert inner.calls == 2, "the over-budget call must never reach the adapter"


def test_the_budget_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        BudgetedGateway(_CountingGateway(), max_calls=0)


def test_remaining_counts_down_and_never_goes_negative() -> None:
    gateway = BudgetedGateway(_CountingGateway(), max_calls=1)
    assert gateway.remaining == 1
    _call(gateway)
    assert gateway.remaining == 0
    with pytest.raises(BudgetExceeded):
        _call(gateway)
    assert gateway.remaining == 0


def test_a_budgeted_fake_is_still_a_fake() -> None:
    """Otherwise a budgeted stand-in run would be published as real-model evidence."""
    gateway = BudgetedGateway(FakeModelGateway(), max_calls=5)
    assert classify_gateway(gateway) is PredictionSource.FAKE_MODEL
    assert gateway.prediction_source is PredictionSource.FAKE_MODEL


def test_a_budgeted_recording_is_still_a_replay() -> None:
    recording = Recording(dataset_name="d", dataset_version="v1")
    gateway = BudgetedGateway(RecordedModelGateway(recording), max_calls=5)
    assert classify_gateway(gateway) is PredictionSource.RECORDED_REPLAY


def test_classify_gateway_unwraps_a_chain_of_decorators() -> None:
    inner = FakeModelGateway()
    twice = BudgetedGateway(BudgetedGateway(inner, max_calls=5), max_calls=5)
    assert underlying_gateway(twice) is inner
    assert classify_gateway(twice) is PredictionSource.FAKE_MODEL


def test_an_unknown_gateway_is_still_a_live_call() -> None:
    """Unwrapping must not turn an unrecognised adapter into a known one."""
    assert classify_gateway(BudgetedGateway(_CountingGateway(), max_calls=5)) is (
        PredictionSource.LIVE_MODEL
    )


def test_the_budget_forwards_the_usage_counter() -> None:
    """A budget must not hide what the run spent."""
    inner = _CountingGateway()
    assert gateway_usage(BudgetedGateway(inner, max_calls=5)) is inner.usage
    assert gateway_usage(BudgetedGateway(inner, max_calls=5)).total_tokens == 15  # type: ignore[union-attr]


def test_wrapping_does_not_hide_the_incomplete_recording_check() -> None:
    """The guard is an ``isinstance`` test, so a wrapper would silently disable it."""
    empty = Recording(dataset_name="empty", dataset_version="v1")
    gateway = BudgetedGateway(RecordedModelGateway(empty), max_calls=100)
    with pytest.raises(EvaluationSetupError) as error:
        asyncio.run(
            run_corpus(
                corpus=BUILTIN_CORPUS,
                cases=BUILTIN_CORPUS.split(Split.DEV),
                gateway=gateway,
                embedding_gateway=FakeEmbeddingGateway(dimension=32),
                config=_CONFIG,
            )
        )
    assert error.value.code == INCOMPLETE_RECORDING


def test_a_run_that_runs_out_of_budget_does_not_report_a_result() -> None:
    """A truncated run must not report rates over whatever fit inside the budget."""
    gateway = BudgetedGateway(FakeModelGateway(), max_calls=3)
    with pytest.raises(BudgetExceeded):
        asyncio.run(
            run_corpus(
                corpus=BUILTIN_CORPUS,
                cases=BUILTIN_CORPUS.split(Split.DEV),
                gateway=gateway,
                embedding_gateway=FakeEmbeddingGateway(dimension=32),
                config=_CONFIG,
            )
        )
    assert gateway.calls == 3


def test_a_budget_larger_than_the_run_is_not_a_stop_condition() -> None:
    """A budget that was never reached must not change the outcome."""
    cases = BUILTIN_CORPUS.split(Split.DEV)
    gateway = BudgetedGateway(FakeModelGateway(), max_calls=len(cases) * 2)
    run = asyncio.run(
        run_corpus(
            corpus=BUILTIN_CORPUS,
            cases=cases,
            gateway=gateway,
            embedding_gateway=FakeEmbeddingGateway(dimension=32),
            config=_CONFIG,
        )
    )
    assert len(run.measured_cases) == len(cases)
    assert run.source is PredictionSource.FAKE_MODEL
    assert gateway.calls == len(cases)
