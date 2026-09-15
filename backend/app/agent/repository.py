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

    async def list_stale_runs(
        self,
        *,
        run_type: RunType,
        statuses: tuple[RunStatus, ...],
        older_than: datetime,
        limit: int,
    ) -> list[AgentRun]:
        """Runs of ``run_type`` sitting in ``statuses`` since before ``older_than``.

        The republish scan's read side (§14.4). It deliberately filters on
        ``updated_at`` — the last moment the row moved — rather than on a
        heartbeat, because a run that is genuinely being worked on is *not*
        quiet: the claim bumps it as it writes events. Ordered by ``updated_at``
        so the oldest backlog is drained first, and capped so one cycle cannot
        publish an unbounded number of tasks.
        """
        ...

    async def refresh_for_poll(self) -> None:
        """Release the frozen snapshot before a poll read so the worker's commit is seen.

        The SSE stream re-reads PostgreSQL every heartbeat (§13.2). Under a long-lived
        session the first query opens a READ COMMITTED transaction whose snapshot is
        frozen for its whole life and ``session.get`` returns the cached instance, so
        commits from the worker would never be observed and the run would never appear
        to finish. We therefore end the current transaction and clear the identity map
        on the *same* session before each poll (see ``SqlAgentRunRepository``). In-memory

        adapters have no snapshot and are no-ops.
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

    async def list_stale_runs(
        self,
        *,
        run_type: RunType,
        statuses: tuple[RunStatus, ...],
        older_than: datetime,
        limit: int,
    ) -> list[AgentRun]:
        async with self._lock:
            stale = [
                run
                for run in self._runs.values()
                if run.run_type is run_type
                and run.status in statuses
                and run.updated_at <= older_than
            ]
            stale.sort(key=lambda run: (run.updated_at, str(run.id)))
            return stale[:limit]

    async def aclose(self) -> None:
        return None


class SqlAgentRunRepository:
    """PostgreSQL adapter; the sequence increment is a row-locked UPDATE.

    Two session modes:

    * ``session=`` (default) — borrow one session for the whole operation. Used by
      request handlers and the Celery worker, which own the transaction/commit.
    * ``session_factory=`` — the SSE stream holds one session for the whole stream and
      re-reads the database every heartbeat (§13.2). A READ COMMITTED transaction opened
      by the first query freezes its snapshot until the transaction ends, and the ORM
      identity map caches the loaded ``AgentRun``, so without intervention the replay
      read would never see the worker's completion. ``refresh_for_poll`` therefore ends
      the current transaction (``rollback``) and clears the identity map (``expire_all``)
      on that *same* session, so the next read starts a fresh transaction whose snapshot
      reflects the worker's commit and ``get_run`` is forced to re-SELECT. The session is
      **never** closed/reopened between polls: returning the borrowed asyncpg connection to
      the pool every poll churned the small pool (DB_POOL_SIZE=5) and left non-checked-in
      connections the GC tore down mid-stream, killing the SSE stream before it could emit
      the terminal frame (recruitment-flow.spec.ts stayed at 0 candidates across CI runs
      34864742879 / 34866789410 / 34868455712).
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
        # Factory mode only: release the frozen READ COMMITTED snapshot and clear the
        # ORM identity map IN PLACE so the next read observes the worker's committed
        # completion (§13.2).
        #
        # The SSE stream holds one session for its whole life. The first query opened a
        # transaction whose snapshot is frozen for that transaction; a subsequent
        # ``session.get`` would otherwise return the cached (stale) RUNNING instance and
        # the run would never appear to finish. ``rollback()`` ends that transaction, so
        # the next query autobegins a NEW READ COMMITTED transaction whose snapshot
        # reflects the worker's commit; ``expire_all()`` invalidates the cached instance so
        # ``get_run`` re-SELECTs rather than returning the stale one.
        #
        # We must NOT close()+reopen() the session here: returning the borrowed asyncpg
        # connection to the pool every poll churns a small pool (DB_POOL_SIZE=5) and leaves
        # "non-checked-in" connections the garbage collector tears down mid-stream, killing
        # the SSE stream before it emits the terminal frame (recruitment-flow.spec.ts stayed
        # at 0 candidates across CI runs 34864742879 / 34866789410 / 34868455712).
        if self._factory is None:
            return
        await self._session.rollback()
        self._session.expire_all()

    async def aclose(self) -> None:
        # Factory mode holds one session for the whole stream; release it on close.
        if self._factory is not None:
            await self._session.close()

    async def list_stale_runs(
        self,
        *,
        run_type: RunType,
        statuses: tuple[RunStatus, ...],
        older_than: datetime,
        limit: int,
    ) -> list[AgentRun]:
        """Quiet runs of one kind, oldest first (republish scan read side, §14.4).

        No ``FOR UPDATE``: this is a *candidate list*, not a claim. Deciding
        whether a candidate may actually be re-delivered needs the run's
        checkpoint and approvals, which the service reads per record under the
        proper lock order — taking a batch lock here would hold row locks across
        those reads for no benefit.
        """
        result = await self._session.execute(
            select(AgentRun)
            .where(
                AgentRun.run_type == run_type,
                AgentRun.status.in_(statuses),
                AgentRun.updated_at <= older_than,
            )
            .order_by(AgentRun.updated_at, AgentRun.id)
            .limit(limit)
        )
        return list(result.scalars().all())
