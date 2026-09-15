"""Delivery port for Run execution tasks (FIN-005, detailed design §14.1/§14.4).

Runs are recorded by the API and executed by a worker, so the API needs a way to
hand one over without owning Celery. Same shape as ``documents.parse_service``'s
``ParseEnqueuer``: a ``Protocol`` the request path depends on, a Celery
implementation for production, and an in-memory fake in tests.

The delivery is *not* the guarantee. Redis can drop a publication and Celery
delivers at least once, so a run that was never published — or published twice —
must still land in exactly one terminal state. That guarantee lives in the
database claim (``agent.tasks.claim_run``); this module only carries the work.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol
from uuid import UUID

from celery import Celery  # type: ignore[import-untyped]


class ExecutionIntent(StrEnum):
    """Why this delivery was published — the claim's first question (§5.6, §14.1).

    Lives here rather than in ``agent.tasks`` because it is the contract *between*
    the two sides: the request path decides it, the worker consumes it. Keeping it
    on the port lets ``tasks`` import ``enqueuer`` without a cycle.

    The two values must stay distinct. A ``FAILED`` run is not by itself an
    invitation to execute: if ``START`` accepted it, an at-least-once redelivery —
    or FIN-006's republish scan, which sweeps ``CREATED`` — would silently re-run
    something a human was supposed to retry explicitly. Conversely ``RETRY`` must
    not accept anything but ``FAILED``, or "retry" would become a way to restart a
    run that is mid-pass or sitting at an approval gate.
    """

    START = "start"
    RETRY = "retry"
    RESUME = "resume"


def operation_key(run_id: UUID, attempt: int, resume_version: str | None = None) -> str:
    """The §14.1 operation key identifying one execution slice of a Run.

    Stable across redelivery of the same slice, and distinct across slices — that
    is what lets a worker, a log line, and the result backend agree on *which*
    attempt of *which* run a message belongs to.

    Two shapes exist because the two run types retry differently (§5.6):

    * ``MatchRun`` retries the same run row with ``attempt + 1``, so
      ``run_id`` + ``attempt`` is already unique — ``resume_version`` is omitted.
    * ``ApplicationRun`` resumes from a checkpoint, so the checkpoint it continues
      from is appended as well; two resumes of one attempt are different slices.
    """
    segments = [str(run_id), str(attempt)]
    if resume_version is not None:
        segments.append(resume_version)
    return ":".join(segments)


class RunEnqueuer(Protocol):
    """Hands a persisted Run to the worker that will execute it."""

    def enqueue_match_run(
        self, run_id: UUID, *, attempt: int, intent: ExecutionIntent
    ) -> None: ...

    def enqueue_application_run(
        self,
        run_id: UUID,
        *,
        attempt: int,
        intent: ExecutionIntent,
        actor_id: UUID,
        resume_version: str | None = None,
        approval_id: UUID | None = None,
    ) -> None: ...


class CeleryRunEnqueuer:
    """Production ``RunEnqueuer`` that delivers a named Celery task.

    ``task_id`` is set to the operation key so a redelivery is recognisable in the
    worker log and a republish (§14.4, FIN-006) is observable through the result
    backend. It is *not* an exactly-once mechanism: the Redis broker does not
    deduplicate by task id, and the database claim remains the guard.

    ``intent`` travels as a task argument, not as a second task name: one task
    whose behaviour is decided by the claim keeps a single place where "may this
    delivery run?" is answered (§14.1's operation key names the slice; the intent
    names what is being asked of it).
    """

    MATCH_RUN_TASK = "agent.execute_match_run"
    APPLICATION_RUN_TASK = "agent.execute_application_run"

    def __init__(self, celery_app: Celery) -> None:
        self._celery_app = celery_app

    def enqueue_match_run(
        self, run_id: UUID, *, attempt: int, intent: ExecutionIntent
    ) -> None:
        self._celery_app.send_task(
            self.MATCH_RUN_TASK,
            args=[str(run_id), intent.value],
            task_id=operation_key(run_id, attempt),
        )

    def enqueue_application_run(
        self,
        run_id: UUID,
        *,
        attempt: int,
        intent: ExecutionIntent,
        actor_id: UUID,
        resume_version: str | None = None,
        approval_id: UUID | None = None,
    ) -> None:
        # A resume carries both the durable checkpoint version and the approval
        # that authorised it. The worker re-reads both facts; task arguments are
        # routing hints, never an authority source.
        self._celery_app.send_task(
            self.APPLICATION_RUN_TASK,
            args=[
                str(run_id),
                intent.value,
                str(actor_id),
                resume_version,
                str(approval_id) if approval_id is not None else None,
            ],
            task_id=operation_key(run_id, attempt, resume_version),
        )
