"""Event notifier for SSE fan-out (IMP-025, detailed design §13.2/§13.5).

The publish path is intentionally thin: a writer appends an ``AgentEvent`` to
PostgreSQL and then publishes ``run_id + latest_sequence`` to this notifier. The
message carries *no business data* — a subscriber only wakes up and re-reads the
database. If a publish is lost (Redis down, network blip), the SSE loop's periodic
heartbeat re-reads PostgreSQL and still discovers the new sequence, so the system
degrades to at-worst one heartbeat-period of staleness rather than dropping events.

Two adapters ship here:

* ``RedisEventNotifier`` — production fan-out via Redis Pub/Sub (one channel per
  run). Multiple API workers can each subscribe and wake their local connections.
* ``InMemoryEventNotifier`` — process-local broadcast for hermetic unit tests and
  single-process local runs; shares the same protocol the SSE endpoint awaits on.
"""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis


def _channel(run_id: UUID) -> str:
    return f"run:{run_id}"


class EventNotifier(Protocol):
    """Wake SSE connections for a run when a new event is committed."""

    def subscribe(self, run_id: UUID) -> Subscription:
        """Return a subscription the SSE loop awaits on for new events."""
        ...

    async def publish(self, run_id: UUID, sequence: int) -> None:
        """Notify subscribers that a run reached ``sequence`` (no payload)."""
        ...


class Subscription(Protocol):
    """Awaitable handle bound to one SSE connection."""

    async def wait(self, timeout: float) -> None:
        """Block until a publish arrives or ``timeout`` seconds elapse."""
        ...

    async def aclose(self) -> None:
        """Release the underlying subscription resources."""
        ...


class NoOpEventNotifier:
    """Silent notifier; the SSE loop relies solely on heartbeat polling."""

    def subscribe(self, run_id: UUID) -> Subscription:
        return _NoOpSubscription()

    async def publish(self, run_id: UUID, sequence: int) -> None:  # noqa: D401
        return None


class _NoOpSubscription:
    async def wait(self, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return None

    async def aclose(self) -> None:
        return None


class InMemoryEventNotifier:
    """Process-local pub/sub for tests and single-process local runs.

    Tracks the latest published sequence per run so a ``wait`` that begins *after*
    a publish returns immediately (no lost wake-up under single-threaded asyncio).
    """

    def __init__(self) -> None:
        self._latest: dict[UUID, int] = {}
        self._waiters: dict[UUID, list[asyncio.Future[None]]] = {}
        self._lock = asyncio.Lock()

    def subscribe(self, run_id: UUID) -> Subscription:
        return _InMemorySubscription(self, run_id)

    async def publish(self, run_id: UUID, sequence: int) -> None:
        async with self._lock:
            self._latest[run_id] = max(self._latest.get(run_id, -1), sequence)
            waiters = self._waiters.pop(run_id, [])
        for future in waiters:
            if not future.done():
                future.set_result(None)


class _InMemorySubscription:
    def __init__(self, owner: InMemoryEventNotifier, run_id: UUID) -> None:
        self._owner = owner
        self._run_id = run_id
        self._seen = owner._latest.get(run_id, -1)

    async def wait(self, timeout: float) -> None:
        # A publish that arrived after subscribe (or after the last wait) is already
        # reflected in _latest, so wake immediately instead of blocking.
        if self._owner._latest.get(self._run_id, -1) > self._seen:
            return None
        loop = asyncio.get_event_loop()
        future: asyncio.Future[None] = loop.create_future()
        self._owner._waiters.setdefault(self._run_id, []).append(future)
        try:
            await asyncio.wait_for(future, timeout)
        except TimeoutError:
            pass
        finally:
            self._seen = self._owner._latest.get(self._run_id, -1)

    async def aclose(self) -> None:
        return None


class RedisEventNotifier:
    """Production fan-out via Redis Pub/Sub (one channel per run)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def subscribe(self, run_id: UUID) -> Subscription:
        return _RedisSubscription(self._redis, _channel(run_id))

    async def publish(self, run_id: UUID, sequence: int) -> None:
        # Fire-and-forget: the message carries only the sequence; a lost publish is
        # recovered by the SSE heartbeat polling PostgreSQL (§13.2).
        await self._redis.publish(_channel(run_id), str(sequence))


class _RedisSubscription:
    def __init__(self, redis: Redis, channel: str) -> None:
        self._redis = redis
        self._channel = channel
        self._pubsub = redis.pubsub()
        self._ready = False

    async def wait(self, timeout: float) -> None:
        if not self._ready:
            await self._pubsub.subscribe(self._channel)
            self._ready = True
        # Enforce the heartbeat bound with asyncio.wait_for rather than trusting
        # get_message's own timeout. In some redis.asyncio versions (and in the
        # shared-Redis e2e stack) get_message ignored its timeout and blocked until
        # a real message arrived; the cross-process Pub/Sub wake from the worker was
        # also unreliable, so the SSE loop hung here and the §13.2 PostgreSQL
        # re-read never fired. wait_for guarantees the loop returns after `timeout`
        # and re-reads the run, discovering RUN_COMPLETED.
        try:
            await asyncio.wait_for(
                self._pubsub.get_message(ignore_subscribe_messages=True),
                timeout,
            )
        except TimeoutError:
            return
        except Exception:
            # A broken Pub/Sub read (e.g. a cancelled socket read) must not abort the
            # stream: the SSE loop re-reads PostgreSQL and still discovers the terminal
            # state (§13.2). A genuine external cancellation propagates as
            # CancelledError and is intentionally not swallowed.
            return

    async def aclose(self) -> None:
        try:
            await self._pubsub.unsubscribe(self._channel)
        finally:
            await self._pubsub.aclose()  # type: ignore[no-untyped-call]
