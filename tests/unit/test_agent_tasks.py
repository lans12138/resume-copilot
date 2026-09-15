"""Hermetic tests for the Run worker's claim policy (FIN-005, §14).

The interesting part of an at-least-once worker is not *how* it executes but
*when it refuses to*. Those refusals live in :func:`decide_claim`, a pure function,
so the whole policy can be pinned without a broker, a database, or a model — the
end-to-end wiring is covered by ``tests/validate_application_entry.ps1``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.enqueuer import CeleryRunEnqueuer, operation_key
from backend.app.agent.models import AgentRun, RunStatus, RunType
from backend.app.agent.tasks import (
    ClaimDecision,
    ExecutionIntent,
    _execute_match_run_async,
    claim_run,
    decide_claim,
)
from backend.app.infrastructure.runtime import RuntimeResources


def _run(*, status: RunStatus, cancel_requested: bool = False) -> AgentRun:
    return AgentRun(
        id=uuid4(),
        thread_id=uuid4().hex,
        run_type=RunType.MATCH,
        status=status,
        attempt=1,
        next_event_sequence=0,
        version=1,
        config_snapshot_json={"job_id": str(uuid4())},
        cancel_requested_at=(
            datetime.now(tz=UTC) if cancel_requested else None
        ),
    )


class _FakeSession:
    """Records the claim's lock request; nothing else is reached on a skip."""

    def __init__(self, run: AgentRun | None) -> None:
        self._run = run
        self.get_calls: list[dict[str, Any]] = []
        self.rolled_back = False
        self.closed = False

    async def get(self, entity: Any, ident: Any, **kwargs: Any) -> AgentRun | None:
        self.get_calls.append({"entity": entity, "ident": ident, **kwargs})
        return self._run

    async def rollback(self) -> None:
        self.rolled_back = True

    async def close(self) -> None:
        self.closed = True


class _FakeResources:
    def __init__(self, session: Any) -> None:
        self.session = session
        self.closed = False

    def session_factory(self) -> Any:
        return self.session

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("status", "cancel_requested", "intent", "expected"),
    [
        (None, False, ExecutionIntent.START, ClaimDecision.NOT_FOUND),
        # Terminal runs never move again, whatever the caller wants.
        (RunStatus.CREATED, True, ExecutionIntent.START, ClaimDecision.CANCELLED_BEFORE_START),
        (RunStatus.RUNNING, True, ExecutionIntent.RETRY, ClaimDecision.CANCELLED_BEFORE_START),
        (RunStatus.COMPLETED, False, ExecutionIntent.START, ClaimDecision.ALREADY_TERMINAL),
        (RunStatus.CANCELLED, False, ExecutionIntent.START, ClaimDecision.ALREADY_TERMINAL),
        (RunStatus.FAILED, False, ExecutionIntent.START, ClaimDecision.ALREADY_TERMINAL),
        # START only ever begins a run nobody has begun.
        (RunStatus.CREATED, False, ExecutionIntent.START, ClaimDecision.CLAIMED),
        (RunStatus.RUNNING, False, ExecutionIntent.START, ClaimDecision.ALREADY_RUNNING),
        (RunStatus.INTERRUPTED, False, ExecutionIntent.START, ClaimDecision.NOT_STARTABLE),
        # RETRY is the only door back into execution from FAILED (§5.6).
        (RunStatus.FAILED, False, ExecutionIntent.RETRY, ClaimDecision.CLAIMED),
        (RunStatus.CREATED, False, ExecutionIntent.RETRY, ClaimDecision.NOT_RETRYABLE),
        (RunStatus.RUNNING, False, ExecutionIntent.RETRY, ClaimDecision.NOT_RETRYABLE),
        # RESUME is admitted only after ApprovalService durably records RUNNING.
        (RunStatus.RUNNING, False, ExecutionIntent.RESUME, ClaimDecision.CLAIMED),
        (
            RunStatus.WAITING_APPROVAL,
            False,
            ExecutionIntent.RESUME,
            ClaimDecision.PAUSED_FOR_APPROVAL,
        ),
        (RunStatus.CREATED, False, ExecutionIntent.RESUME, ClaimDecision.NOT_RESUMABLE),
    ],
)
def test_claim_policy_branches(
    status: RunStatus | None,
    cancel_requested: bool,
    intent: ExecutionIntent,
    expected: ClaimDecision,
) -> None:
    assert (
        decide_claim(status=status, cancel_requested=cancel_requested, intent=intent) is expected
    )


def test_paused_run_is_not_startable_by_a_plain_delivery() -> None:
    """The human gate must hold against an ordinary re-delivery.

    A run parked at ``WAITING_APPROVAL`` is the one state where executing again
    would perform the side effect the approval exists to withhold, so neither
    intent may claim it: ``RETRY`` is rejected by status, and ``START`` must say
    *paused* rather than falling through to a generic refusal — the reason is what
    an operator reads when a run appears stuck.
    """
    assert (
        decide_claim(
            status=RunStatus.WAITING_APPROVAL,
            cancel_requested=False,
            intent=ExecutionIntent.START,
        )
        is ClaimDecision.PAUSED_FOR_APPROVAL
    )
    assert (
        decide_claim(
            status=RunStatus.WAITING_APPROVAL,
            cancel_requested=False,
            intent=ExecutionIntent.RETRY,
        )
        is ClaimDecision.NOT_RETRYABLE
    )


def test_claim_locks_the_run_row_before_deciding() -> None:
    """``with_for_update`` is what makes the status check non-racy."""

    async def scenario() -> tuple[AgentRun | None, ClaimDecision, _FakeSession]:
        run = _run(status=RunStatus.CREATED)
        session = _FakeSession(run)
        claimed, decision = await claim_run(
            cast(AsyncSession, session), run.id, intent=ExecutionIntent.START
        )
        return claimed, decision, session

    claimed, decision, session = asyncio.run(scenario())

    assert claimed is not None
    assert decision is ClaimDecision.CLAIMED
    assert session.get_calls == [
        {"entity": AgentRun, "ident": claimed.id, "with_for_update": True}
    ]


def test_claim_reads_cancellation_from_the_row_not_the_caller() -> None:
    """The cancel flag is authoritative in the database, never in the message."""

    async def scenario() -> ClaimDecision:
        run = _run(status=RunStatus.CREATED, cancel_requested=True)
        _claimed, decision = await claim_run(
            cast(AsyncSession, _FakeSession(run)), run.id, intent=ExecutionIntent.START
        )
        return decision

    assert asyncio.run(scenario()) is ClaimDecision.CANCELLED_BEFORE_START


def test_operation_key_is_stable_and_slice_scoped() -> None:
    """§14.1: MatchRun keys on run+attempt; ApplicationRun adds the resume version."""
    run_id = UUID("11111111-2222-3333-4444-555555555555")

    assert operation_key(run_id, 1) == f"{run_id}:1"
    # A retry of the same run re-enters it with attempt + 1, so redelivery of the
    # first pass and the retry cannot share an identity.
    assert operation_key(run_id, 2) != operation_key(run_id, 1)
    assert operation_key(run_id, 1, "ckpt-0007") == f"{run_id}:1:ckpt-0007"


def test_application_delivery_carries_resume_authority_and_version() -> None:
    class StubCelery:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def send_task(self, name: str, **kwargs: Any) -> None:
            self.calls.append({"name": name, **kwargs})

    celery = StubCelery()
    enqueuer = CeleryRunEnqueuer(cast(Any, celery))
    run_id = uuid4()
    actor_id = uuid4()
    approval_id = uuid4()

    enqueuer.enqueue_application_run(
        run_id,
        attempt=3,
        intent=ExecutionIntent.RESUME,
        actor_id=actor_id,
        resume_version="checkpoint-9",
        approval_id=approval_id,
    )

    assert celery.calls == [
        {
            "name": "agent.execute_application_run",
            "args": [
                str(run_id),
                "resume",
                str(actor_id),
                "checkpoint-9",
                str(approval_id),
            ],
            "task_id": f"{run_id}:3:checkpoint-9",
        }
    ]


def test_worker_skips_a_redelivered_terminal_run() -> None:
    """Item 4 of FIN-005: a duplicate delivery must not reopen a finished run."""
    run = _run(status=RunStatus.COMPLETED)
    session = _FakeSession(run)
    resources = _FakeResources(session)

    result = asyncio.run(
        _execute_match_run_async(cast(RuntimeResources, resources), run.id)
    )

    assert result == {
        "status": "skipped",
        "reason": ClaimDecision.ALREADY_TERMINAL.value,
        "run_id": str(run.id),
    }
    # The claim is rolled back rather than committed, and the loop-bound engine
    # still gets torn down inside the same event loop that built it.
    assert session.rolled_back is True
    assert session.closed is True
    assert resources.closed is True


def test_worker_skips_a_run_that_was_cancelled_before_it_started() -> None:
    run = _run(status=RunStatus.CREATED, cancel_requested=True)
    resources = _FakeResources(_FakeSession(run))

    result = asyncio.run(
        _execute_match_run_async(cast(RuntimeResources, resources), run.id)
    )

    assert result["status"] == "skipped"
    assert result["reason"] == ClaimDecision.CANCELLED_BEFORE_START.value


def test_worker_reports_a_run_that_vanished() -> None:
    run_id = uuid4()
    resources = _FakeResources(_FakeSession(None))

    result = asyncio.run(_execute_match_run_async(cast(RuntimeResources, resources), run_id))

    assert result == {
        "status": "skipped",
        "reason": ClaimDecision.NOT_FOUND.value,
        "run_id": str(run_id),
    }
