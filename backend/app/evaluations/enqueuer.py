"""Delivery port for evaluation runs (FIN-007, §14.1).

Same shape as ``agent.enqueuer`` and ``documents.parse_service``'s
``ParseEnqueuer``: a ``Protocol`` the request path depends on, a Celery
implementation for production, and an in-memory fake for tests. The API records
the run and commits it, *then* publishes — so a dropped publication leaves a
``CREATED`` run that is visibly stuck rather than a request that silently did
nothing (§14.2).

``task_id`` is the run id itself. Unlike a Run, an evaluation has a single
execution slice: there is no attempt counter and no checkpoint to resume from.
A re-submission of the same (dataset, config) is refused by the duplicate guard
rather than given a second slice, and a redelivery of the *same* task is absorbed
by the ``CREATED -> RUNNING`` claim in the service.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from celery import Celery  # type: ignore[import-untyped]


class EvaluationEnqueuer(Protocol):
    """Hands a persisted evaluation run to the worker that will score it."""

    def enqueue_evaluation(self, evaluation_run_id: UUID) -> None: ...


class CeleryEvaluationEnqueuer:
    """Production ``EvaluationEnqueuer`` that delivers ``evaluations.execute``."""

    TASK = "evaluations.execute"

    def __init__(self, celery_app: Celery) -> None:
        self._celery_app = celery_app

    def enqueue_evaluation(self, evaluation_run_id: UUID) -> None:
        self._celery_app.send_task(
            self.TASK,
            args=[str(evaluation_run_id)],
            task_id=str(evaluation_run_id),
        )


__all__ = ["CeleryEvaluationEnqueuer", "EvaluationEnqueuer"]
