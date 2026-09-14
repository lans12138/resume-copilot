"""Agent run and event persistence ports and adapters (IMP-018).

The repository owns one non-obvious concurrency rule: the event sequence is
produced by incrementing ``AgentRun.next_event_sequence`` inside the same
transaction that inserts the ``AgentEvent``. Callers never compute a sequence;
they describe *what happened* and receive the assigned, strictly-ordered
``AgentEvent`` back. That makes concurrent writers (multiple workers, retries)
safe without ``max(sequence)+1`` races.

Two adapters ship here:

* ``InMemoryAgentRunRepository`` uses an ``asyncio.Lock`` to guarantee the
  increment-and-insert is atomic. It powers hermetic unit tests and local runs
  with no database.
* ``SqlAgentRunRepository`` uses ``UPDATE ... SET next_event_sequence = col + 1
  ... RETURNING`` which takes a row lock on the run; this is the production
  path against PostgreSQL and is exercised end-to-end in IMP-030.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.models import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    RunStatus,
    RunType,
)


class AgentRunRepository(Protocol):
    """Persistence contract for runs and their ordered event log."""

    async def save_run(self, run: AgentRun) -> None: ...

    async def get_run(self, run_id: UUID) -> AgentRun | None: ...

    async def append_event(
        self,
        *,
        run_id: UUID,
        run_type: RunType,
        event_type: AgentEventType,
        node: str | None,
        status: str,
        message_key: str,
        safe_payload: dict[str, Any],
    ) -> AgentEvent: ...

    async def set_status(
        self, run_id: UUID, status: RunStatus, *, finished: bool = False
    ) -> None: ...

    async def list_events(self, run_id: UUID) -> list[AgentEvent]: ...

    async def list_events_after(
        self, run_id: UUID, last_sequence: int, limit: int
    ) -> list[AgentEvent]:
        """Replay events with ``sequence > last_sequence`` ordered, capped at limit."""
        ...

    async def get_event_by_sequence(
        self, run_id: UUID, sequence: int
    ) -> AgentEvent | None:
        """Fetch one event by (run_id, sequence); None if it does not belong here."""
        ...

    async def refresh_for_poll(self) -> None:
        """Release the frozen transaction/identity-map snapshot before a poll read.

        The SSE stream re-reads PostgreSQL every heartbeat (§13.2). Under a
        long-lived session the first query opens a transaction whose snapshot is
        frozen for its whole life and ``session.get`` returns the cached instance,
        so commits from the worker would never be observed and the run would never
        appear to finish. In-memory adapters have no snapshot and are no-ops; the
        SQL adapter closes its session and opens a fresh one (see its docstring).
        """
        ...

    async def aclose(self) -> None:
        """Release any open connection; called when the SSE stream terminates."""
        ...


class InMemoryAgentRunRepository:
    """Lock-guarded in-process store; sequence is atomic under concurrency."""

    def __init__(self) -> None:
        self._runs: dict[UUID, AgentRun] = {}
        self._events: list[AgentEvent] = []
        self._lock = asyncio.Lock()

    async def save_run(self, run: AgentRun) -> None:
        async with self._lock:
            self._runs[run.id] = run

    async def get_run(self, run_id: UUID) -> AgentRun | None:
        async with self._lock:
            return self._runs.get(run_id)

    async def append_event(
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
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(f"no run with id {run_id}")
            sequence = run.next_event_sequence
            run.next_event_sequence = sequence + 1
            event = AgentEvent(
                run_id=run_id,
                run_type=run_type,
                sequence=sequence,
                event_type=event_type,
                node=node,
                status=status,
                message_key=message_key,
                safe_payload_json=safe_payload,
            )
            self._events.append(event)
            return event

    async def set_status(
        self, run_id: UUID, status: RunStatus, *, finished: bool = False
    ) -> None:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(f"no run with id {run_id}")
            run.status = status
            if finished:
                run.finished_at = datetime.now(tz=datetime.now().astimezone().tzinfo)

    async def list_events(self, run_id: UUID) -> list[AgentEvent]:
        async with self._lock:
            return [e for e in self._events if e.run_id == run_id]

    async def list_events_after(
        self, run_id: UUID, last_sequence: int, limit: int
    ) -> list[AgentEvent]:
        async with self._lock:
            ordered = sorted(
                (
                    e
                    for e in self._events
                    if e.run_id == run_id and e.sequence > last_sequence
                ),
                key=lambda e: e.sequence,
            )
            return ordered[:limit]

    async def get_event_by_sequence(
        self, run_id: UUID, sequence: int
    ) -> AgentEvent | None:
        async with self._lock:
            for event in self._events:
                if event.run_id == run_id and event.sequence == sequence:
                    return event
            return None

    async def refresh_for_poll(self) -> None:
        # No snapshot to drop: the in-memory store always reflects latest state.
        return None

    async def aclose(self) -> None:
        return None


class SqlAgentRunRepository:
    """PostgreSQL adapter; the sequence increment is a row-locked UPDATE.

    Two session modes:

    * ``session=`` (default) — borrow one session for the whole operation. Used by
      request handlers and the Celery worker, which own the transaction/commit.
    * ``session_factory=`` — the SSE stream re-reads the database every heartbeat
      (§13.2). A single session opened at stream start would freeze its READ
      COMMITTED snapshot for the whole connection; even ``rollback`` + ``expire_all``
      is not enough because the underlying asyncpg connection keeps its snapshot
      until its transaction ends, so a reload through the same connection still saw
      the worker's pre-completion state. In factory mode ``refresh_for_poll`` closes
      the session (returning the connection to the pool) and opens a fresh one, so
      each replay read sees a current snapshot and COMPLETED / RUN_COMPLETED become
      visible within one heartbeat of the worker's commit.
    """

    def __init__(
        self,
        session: AsyncSession | None = None,
        *,
        session_factory: Callable[[], AsyncSession] | None = None,
    ) -> None:
        if session_factory is not None:
            self._factory: Callable[[], AsyncSession] | None = session_factory
            self._session = session_factory()
        elif session is not None:
            self._factory = None
            self._session = session
        else:
            raise TypeError("SqlAgentRunRepository needs session= or session_factory=")

    async def save_run(self, run: AgentRun) -> None:
        self._session.add(run)
        await self._session.flush()

    async def get_run(self, run_id: UUID) -> AgentRun | None:
        return await self._session.get(AgentRun, run_id)

    async def append_event(
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
        # Single statement takes a FOR UPDATE-style row lock on the run and
        # returns the newly assigned sequence atomically.
        result = await self._session.execute(
            text(
                "UPDATE agent_runs "
                "SET next_event_sequence = next_event_sequence + 1 "
                "WHERE id = :rid "
                "RETURNING next_event_sequence"
            ),
            {"rid": run_id},
        )
        row = result.first()
        if row is None:
            raise KeyError(f"no run with id {run_id}")
        sequence = int(row[0])
        event = AgentEvent(
            run_id=run_id,
            run_type=run_type,
            sequence=sequence,
            event_type=event_type,
            node=node,
            status=status,
            message_key=message_key,
            safe_payload_json=safe_payload,
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def set_status(
        self, run_id: UUID, status: RunStatus, *, finished: bool = False
    ) -> None:
        run = await self._session.get(AgentRun, run_id)
        if run is None:
            raise KeyError(f"no run with id {run_id}")
        run.status = status
        if finished:
            run.finished_at = datetime.now(tz=datetime.now().astimezone().tzinfo)
        await self._session.flush()

    async def list_events(self, run_id: UUID) -> list[AgentEvent]:
        result = await self._session.execute(
            select(AgentEvent)
            .where(AgentEvent.run_id == run_id)
            .order_by(AgentEvent.sequence)
        )
        return list(result.scalars().all())

    async def list_events_after(
        self, run_id: UUID, last_sequence: int, limit: int
    ) -> list[AgentEvent]:
        result = await self._session.execute(
            select(AgentEvent)
            .where(AgentEvent.run_id == run_id, AgentEvent.sequence > last_sequence)
            .order_by(AgentEvent.sequence)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_event_by_sequence(
        self, run_id: UUID, sequence: int
    ) -> AgentEvent | None:
        result = await self._session.execute(
            select(AgentEvent).where(
                AgentEvent.run_id == run_id, AgentEvent.sequence == sequence
            )
        )
        return result.scalars().first()

    async def refresh_for_poll(self) -> None:
        # Factory mode only: close the current session (returning its connection to
        # the pool) and open a brand-new one so the next read starts a fresh READ
        # COMMITTED transaction with a current snapshot and observes the worker's
        # committed completion (§13.2). Reusing the same connection — even after
        # rollback + expire_all — still read the worker's pre-completion state
        # through the frozen asyncpg snapshot, so the SSE replay never saw COMPLETED
        # and never emitted the terminal frame (recruitment-flow.spec.ts stayed at 0
        # candidates). Non-streaming callers (session mode) never invoke this.
        if self._factory is None:
            return
        await self._session.close()
        self._session = self._factory()

    async def aclose(self) -> None:
        if self._factory is not None:
            await self._session.close()
