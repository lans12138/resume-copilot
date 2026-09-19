"""A call budget for the explicitly triggered evaluation (PORT-004).

The roadmap asks for the live-model entry point to carry a call budget and a stop
condition, and the reason is that a live evaluation is the only part of this system
that spends money per run. "How many calls is this allowed to make" is therefore a
parameter of the run, not something a reader reconstructs from the bill afterwards.

**The budget is enforced where the call happens.** A wrapper around the gateway is the
only place that sees every call: counting in the runner would miss a stage added
later, and counting after the run would report an overrun rather than prevent one.

**Exceeding the budget stops the run.** It raises
:class:`~backend.app.evaluations.runner.BudgetExceeded`, which the runner treats as a
setup failure rather than a per-case one. A truncated run must not report rates
computed over whatever happened to fit inside the budget.

**Wrapping a gateway must not change its provenance.** A decorator that reported
itself as a live call would let a budgeted *fake* run be published as real-model
evidence, so this wrapper forwards the source and usage of what it wraps, and
:func:`~backend.app.evaluations.runner.classify_gateway` unwraps the chain.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.app.candidates.schemas import CandidateProfileDraft
from backend.app.documents.parsers import ParsedBlock
from backend.app.evaluations.runner import (
    BudgetExceeded,
    PredictionSource,
    classify_gateway,
    underlying_gateway,
)
from backend.app.infrastructure.chat_completion import UsageRecord
from backend.app.infrastructure.model_gateway import ModelGateway


class BudgetedGateway:
    """A ``ModelGateway`` that stops the run after ``max_calls`` calls.

    Forwards ``version``, provenance and usage, so a budgeted run is reported as the
    same kind of run it would have been without the budget.
    """

    def __init__(self, inner: ModelGateway, *, max_calls: int) -> None:
        if max_calls <= 0:
            raise ValueError(f"max_calls must be positive, got {max_calls}")
        self._inner = inner
        self._max_calls = max_calls
        self.calls = 0
        self.version = inner.version

    @property
    def inner(self) -> ModelGateway:
        return self._inner

    @property
    def max_calls(self) -> int:
        return self._max_calls

    @property
    def remaining(self) -> int:
        return max(0, self._max_calls - self.calls)

    @property
    def prediction_source(self) -> PredictionSource:
        """The provenance of the wrapped gateway, not of the wrapper."""
        return classify_gateway(self._inner)

    @property
    def usage(self) -> UsageRecord | None:
        """The wrapped adapter's counter, so a budget does not hide the usage."""
        record = getattr(underlying_gateway(self._inner), "usage", None)
        return record if isinstance(record, UsageRecord) else None

    async def extract_profile(
        self, *, full_text: str, blocks: Sequence[ParsedBlock]
    ) -> CandidateProfileDraft:
        if self.calls >= self._max_calls:
            raise BudgetExceeded(limit=self._max_calls)
        self.calls += 1
        return await self._inner.extract_profile(full_text=full_text, blocks=blocks)


__all__ = ["BudgetedGateway"]
