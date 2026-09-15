"""Run orchestration service (IMP-018).

``RunService`` wires the repository, checkpointer, and engine into the public
lifecycle used by later IMPs (MatchRun / ApplicationRun endpoints, workers,
SSE). It depends only on the ``AgentRunRepository`` and ``Checkpointer``
protocols, so the hermetic unit tests inject the in-memory adapters while
production injects the SQLAlchemy + PostgreSQL checkpointer adapters.

The service never computes an event sequence and never decides graph topology;
it records *what happened* and delegates execution to ``RunEngine``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from backend.app.agent.checkpoint import Checkpointer
from backend.app.agent.engine import InterruptResult, RunEngine, RunGraph
from backend.app.agent.models import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    CheckpointTuple,
    RunStatus,
    RunType,
)
from backend.app.agent.repository import AgentRunRepository
from backend.app.sse.notifier import EventNotifier


class RunService:
    """Create, drive, and recover agent runs."""

    def __init__(
        self,
        repository: AgentRunRepository,
        checkpointer: Checkpointer,
        notifier: EventNotifier | None = None,
    ) -> None:
        self._repository = repository
        self._checkpointer = checkpointer
        self._engine = RunEngine(repository, checkpointer)
        # Optional SSE fan-out: when set, every committed event publishes its
        # run_id + sequence so live SSE connections wake and replay (IMP-025, §13.2).
        self._notifier = notifier

    async def create_run(
        self,
        *,
        run_type: RunType,
        config_snapshot: dict[str, Any],
        run_id: UUID | None = None,
        thread_id: str | None = None,
        attempt: int = 1,
        created_by: UUID | None = None,
        start_immediately: bool = True,
    ) -> AgentRun:
        """Insert a run and its first event.

        ``start_immediately=False`` is the API/worker hand-off form: the request
        commits a durable ``CREATED`` fact and a worker later owns the transition
        to ``RUNNING``. Existing in-process graph callers retain the historical
        eager transition by default.
        """
        run = AgentRun(
            id=run_id or uuid4(),
            thread_id=thread_id or uuid4().hex,
            run_type=run_type,
            status=RunStatus.CREATED,
            attempt=attempt,
            next_event_sequence=0,
            version=1,
            retryable=False,
            config_snapshot_json=config_snapshot,
            created_by=created_by,
        )
        await self._repository.save_run(run)
        await self._append_event(
            run_id=run.id,
            run_type=run_type,
            event_type=AgentEventType.RUN_CREATED,
            node=None,
            status=RunStatus.CREATED.value,
            message_key="run.created",
            safe_payload={"thread_id": run.thread_id},
        )
        if start_immediately:
            await self._repository.set_status(run.id, RunStatus.RUNNING)
        return run

    async def run_graph(
        self, run: AgentRun, graph: RunGraph, initial_state: dict[str, Any]
    ) -> InterruptResult | None:
        """Execute the graph from the start; returns an interrupt handle or None."""
        return await self._engine.execute(run, graph, initial_state)

    async def resume_run(
        self, run: AgentRun, graph: RunGraph, *, state_override: dict[str, Any] | None = None
    ) -> InterruptResult | None:
        """Continue a paused run from its checkpoint; raises if none exists."""
        return await self._engine.resume(run, graph, state_override=state_override)

    async def get_run(self, run_id: UUID) -> AgentRun | None:
        return await self._repository.get_run(run_id)

    async def get_latest_checkpoint(self, run: AgentRun) -> CheckpointTuple | None:
        """Return the durable resume point for a run, if one exists."""
        return await self._checkpointer.get(run.thread_id, "", "")

    async def list_events(self, run_id: UUID) -> list[AgentEvent]:
        events = await self._repository.list_events(run_id)
        return sorted(events, key=lambda e: e.sequence)

    async def mark_failed(
        self,
        run: AgentRun,
        *,
        reason: str,
        retryable: bool = False,
        error_code: str | None = None,
        failed_node: str | None = None,
    ) -> None:
        await self._append_event(
            run_id=run.id,
            run_type=run.run_type,
            event_type=AgentEventType.RUN_FAILED,
            node=failed_node,
            status=RunStatus.FAILED.value,
            message_key="run.failed",
            safe_payload={"reason": reason, "error_code": error_code, "retryable": retryable},
        )
        run.retryable = retryable
        if error_code is not None:
            run.error_code = error_code
        if failed_node is not None:
            run.failed_node = failed_node
        await self._repository.set_status(run.id, RunStatus.FAILED, finished=True)

    async def _append_event(
        self,
        *,
        run_id: UUID,
        run_type: RunType,
        event_type: AgentEventType,
        node: str | None,
        status: str,
        message_key: str,
        safe_payload: dict[str, Any],
    ) -> AgentEvent:
        """Append an event, then publish its sequence to the SSE notifier."""
        event = await self._repository.append_event(
            run_id=run_id,
            run_type=run_type,
            event_type=event_type,
            node=node,
            status=status,
            message_key=message_key,
            safe_payload=safe_payload,
        )
        if self._notifier is not None:
            await self._notifier.publish(run_id, event.sequence)
        return event

    async def cancel_run(self, run: AgentRun, *, reason: str) -> None:
        await self._append_event(
            run_id=run.id,
            run_type=run.run_type,
            event_type=AgentEventType.RUN_CANCELLED,
            node=None,
            status=RunStatus.CANCELLED.value,
            message_key="run.cancelled",
            safe_payload={"reason": reason},
        )
        await self._repository.set_status(run.id, RunStatus.CANCELLED, finished=True)

    async def emit_status(
        self,
        run: AgentRun,
        *,
        status: RunStatus,
        message_key: str,
        node: str | None = None,
        safe_payload: dict[str, Any] | None = None,
    ) -> None:
        """Write a STATUS_CHANGED event and set the run status in one step."""
        await self._append_event(
            run_id=run.id,
            run_type=run.run_type,
            event_type=AgentEventType.STATUS_CHANGED,
            node=node,
            status=status.value,
            message_key=message_key,
            safe_payload=safe_payload or {},
        )
        await self._repository.set_status(run.id, status)

    async def complete_run(
        self, run: AgentRun, *, reason: str | None, message_key: str = "run.completed"
    ) -> None:
        """Mark a run COMPLETED with a terminal event (finished_at set)."""
        await self._append_event(
            run_id=run.id,
            run_type=run.run_type,
            event_type=AgentEventType.RUN_COMPLETED,
            node=None,
            status=RunStatus.COMPLETED.value,
            message_key=message_key,
            safe_payload={"reason": reason} if reason else {},
        )
        await self._repository.set_status(run.id, RunStatus.COMPLETED, finished=True)
