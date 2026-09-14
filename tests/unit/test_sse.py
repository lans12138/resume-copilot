"""IMP-025: SSE replay, continuous authorization, and live streaming (G5).

Hermetic tests prove the detailed design §13 / §19.6 contract without Redis or a
real database: an in-process ``InMemoryEventNotifier`` stands in for the Redis
Pub/Sub fan-out, and ``InMemoryAgentRunRepository`` is the event log. A fake
``authorize_job`` flips access mid-test to exercise online revocation.

Covered:
* replay from start with no loss / no duplicate (§19.6.1);
* ``Last-Event-ID`` cursor replays only later events (§13.3.3);
* invalid / foreign cursor rejected 400 INVALID_EVENT_CURSOR (§13.3.2);
* unknown run rejected 404 (§12);
* a live publish wakes the stream promptly (the notify path);
* a lost notify is still discovered by the heartbeat poll (§13.2 safety net);
* mid-stream revocation stops further business events and closes with
  SSE_AUTH_REVOKED (§13.4 / §19.6.4-5);
* a terminal run closes after a final heartbeat (§13.3.6);
* match-run job resolution reads job_id from the run config snapshot (§12).
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

from backend.app.agent.checkpoint import InMemoryCheckpointer
from backend.app.agent.models import AgentEventType, AgentRun, RunStatus, RunType
from backend.app.agent.repository import InMemoryAgentRunRepository
from backend.app.agent.service import RunService
from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError
from backend.app.sse.notifier import InMemoryEventNotifier, _RedisSubscription
from backend.app.sse.service import SseService

HEARTBEAT = 0.05  # seconds — small so heartbeat-driven behaviours are prompt in tests


def _actor() -> Actor:
    return Actor(user_id=uuid4(), username="tester", role=UserRole.HIRING_MANAGER)


def _application_run_service(
    agent_repo: InMemoryAgentRunRepository, notifier: InMemoryEventNotifier
) -> RunService:
    return RunService(agent_repo, InMemoryCheckpointer(), notifier=notifier)


def _sse_service(
    agent_repo: InMemoryAgentRunRepository,
    notifier: InMemoryEventNotifier,
    *,
    access_granted: bool = True,
) -> tuple[SseService, dict[str, bool]]:
    state: dict[str, bool] = {"granted": access_granted}

    async def authorize_job(actor: Actor, job_id: UUID) -> None:
        if not state["granted"]:
            raise AppError(
                code="FORBIDDEN", http_status=403, safe_message="当前用户无权查看该岗位"
            )

    svc = SseService(
        agent_repo,
        authorize_job,
        notifier,
        heartbeat_seconds=HEARTBEAT,
        batch_size=100,
        retry_milliseconds=1000,
    )
    return svc, state


async def _create_run(
    run_service: RunService, *, run_type: RunType = RunType.APPLICATION
) -> tuple[AgentRun, UUID]:
    job_id = uuid4()
    run_id = uuid4()
    run = await run_service.create_run(
        run_type=run_type,
        config_snapshot={"job_id": str(job_id), "extra": "x"},
        run_id=run_id,
        thread_id=run_id.hex,
    )
    return run, job_id


def _sequences(text: str) -> list[int]:
    out: list[int] = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("id: "):
                out.append(int(line[4:]))
    return out


def _event_types(text: str) -> list[str]:
    out: list[str] = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("event: "):
                out.append(line[7:])
    return out


async def _drain(gen: Any, timeout: float) -> str:
    """Collect every SSE frame the generator yields, bounded by ``timeout``."""
    chunks: list[str] = []
    try:
        async with asyncio.timeout(timeout):
            async for chunk in gen:
                chunks.append(chunk)
    except TimeoutError:
        pass
    return "".join(chunks)


# --------------------------------------------------------------------------- #
def test_replay_from_start_has_no_loss_or_duplication() -> None:
    asyncio.run(_replay_no_loss())


async def _replay_no_loss() -> None:
    notifier = InMemoryEventNotifier()
    agent_repo = InMemoryAgentRunRepository()
    run_service = _application_run_service(agent_repo, notifier)
    svc, _ = _sse_service(agent_repo, notifier)

    run, _job_id = await _create_run(run_service)  # seq 0: RUN_CREATED
    await run_service.emit_status(run, status=RunStatus.RUNNING, message_key="r.running")
    await run_service.emit_status(run, status=RunStatus.WAITING_APPROVAL, message_key="r.wa")
    await run_service.complete_run(run, reason="done")  # terminal: seq 3

    text = await _drain(svc.stream(_actor(), run.id, -1), timeout=1.0)
    assert _sequences(text) == [0, 1, 2, 3]
    assert "SSE_AUTH_REVOKED" not in text
    assert text.endswith(":\n\n")  # final heartbeat closes the stream


def test_last_event_id_replays_only_later_events() -> None:
    asyncio.run(_replay_cursor())


async def _replay_cursor() -> None:
    notifier = InMemoryEventNotifier()
    agent_repo = InMemoryAgentRunRepository()
    run_service = _application_run_service(agent_repo, notifier)
    svc, _ = _sse_service(agent_repo, notifier)

    run, _job_id = await _create_run(run_service)
    for _ in range(3):
        await run_service.emit_status(run, status=RunStatus.RUNNING, message_key="r.running")
    # Sequences are 0 (RUN_CREATED) + 1,2,3 (status changes).

    # Cursor at sequence 2 -> only sequence 3 is replayed.
    text = await _drain(svc.stream(_actor(), run.id, 2), timeout=1.0)
    assert _sequences(text) == [3]


def test_invalid_cursor_rejected_400() -> None:
    asyncio.run(_invalid_cursor())


async def _invalid_cursor() -> None:
    notifier = InMemoryEventNotifier()
    agent_repo = InMemoryAgentRunRepository()
    run_service = _application_run_service(agent_repo, notifier)
    svc, _ = _sse_service(agent_repo, notifier)

    run, _job_id = await _create_run(run_service)

    # Foreign sequence (not belonging to this run) -> 400.
    try:
        await svc.resolve_initial(_actor(), run.id, "999")
        raise AssertionError("expected INVALID_EVENT_CURSOR")
    except AppError as exc:
        assert exc.code == "INVALID_EVENT_CURSOR"
        assert exc.http_status == 400

    # Non-numeric cursor -> 400.
    try:
        await svc.resolve_initial(_actor(), run.id, "abc")
        raise AssertionError("expected INVALID_EVENT_CURSOR")
    except AppError as exc:
        assert exc.code == "INVALID_EVENT_CURSOR"
        assert exc.http_status == 400


def test_unknown_run_rejected_404() -> None:
    asyncio.run(_unknown_run())


async def _unknown_run() -> None:
    notifier = InMemoryEventNotifier()
    agent_repo = InMemoryAgentRunRepository()
    svc, _ = _sse_service(agent_repo, notifier)
    try:
        await svc.resolve_initial(_actor(), uuid4(), None)
        raise AssertionError("expected RUN_NOT_FOUND")
    except AppError as exc:
        assert exc.code == "RUN_NOT_FOUND"
        assert exc.http_status == 404


def test_live_publish_wakes_stream() -> None:
    asyncio.run(_live_publish())


async def _live_publish() -> None:
    notifier = InMemoryEventNotifier()
    agent_repo = InMemoryAgentRunRepository()
    run_service = _application_run_service(agent_repo, notifier)
    svc, _ = _sse_service(agent_repo, notifier)

    run, _job_id = await _create_run(run_service)  # seq 0
    await run_service.emit_status(run, status=RunStatus.RUNNING, message_key="r.1")  # seq 1

    gen = svc.stream(_actor(), run.id, -1)
    task = asyncio.create_task(_drain(gen, 1.0))
    await asyncio.sleep(0.02)  # first batch (0,1) sent, now waiting on notifier
    await run_service.emit_status(run, status=RunStatus.RUNNING, message_key="r.2")  # seq 2
    await run_service.emit_status(run, status=RunStatus.RUNNING, message_key="r.3")  # seq 3

    text = await task
    assert _sequences(text) == [0, 1, 2, 3]


def test_heartbeat_discovers_events_without_notify() -> None:
    asyncio.run(_heartbeat_discovers())


async def _heartbeat_discovers() -> None:
    # A notifier the writer never publishes to: the stream must rely on the
    # heartbeat polling PostgreSQL (here the in-memory repo) to find new events.
    notifier = InMemoryEventNotifier()
    agent_repo = InMemoryAgentRunRepository()
    run_service = _application_run_service(agent_repo, notifier)
    svc, _ = _sse_service(agent_repo, notifier)

    run, _job_id = await _create_run(run_service)  # seq 0

    gen = svc.stream(_actor(), run.id, -1)
    task = asyncio.create_task(_drain(gen, 0.4))
    await asyncio.sleep(0.02)  # first batch sent, now polling on heartbeat
    # Append directly to the repo, bypassing the notifier (simulates a lost publish).
    await agent_repo.append_event(
        run_id=run.id,
        run_type=run.run_type,
        event_type=AgentEventType.STATUS_CHANGED,
        node=None,
        status=RunStatus.RUNNING.value,
        message_key="r.late",
        safe_payload={},
    )
    text = await task
    assert 1 in _sequences(text)  # discovered by the heartbeat poll


def test_midstream_revocation_closes_stream() -> None:
    asyncio.run(_revocation_closes())


async def _revocation_closes() -> None:
    notifier = InMemoryEventNotifier()
    agent_repo = InMemoryAgentRunRepository()
    run_service = _application_run_service(agent_repo, notifier)
    svc, state = _sse_service(agent_repo, notifier, access_granted=True)

    run, _job_id = await _create_run(run_service)  # seq 0
    await run_service.emit_status(run, status=RunStatus.RUNNING, message_key="r.1")  # seq 1

    gen = svc.stream(_actor(), run.id, -1)
    task = asyncio.create_task(_drain(gen, 0.6))
    await asyncio.sleep(0.02)  # first batch (0,1) sent, now polling
    state["granted"] = False  # JobAssignment revoked mid-connection
    # A publish still happens, but the stream must NOT deliver it after revocation.
    await run_service.emit_status(run, status=RunStatus.RUNNING, message_key="r.2")

    text = await task
    assert "SSE_AUTH_REVOKED" in text
    assert _sequences(text) == [0, 1]  # post-revocation event was withheld
    assert _event_types(text).count("STATUS_CHANGED") == 1


def test_match_run_resolves_job_from_config_snapshot() -> None:
    asyncio.run(_match_run_sse())


async def _match_run_sse() -> None:
    notifier = InMemoryEventNotifier()
    agent_repo = InMemoryAgentRunRepository()
    run_service = _application_run_service(agent_repo, notifier)
    svc, _ = _sse_service(agent_repo, notifier)

    run, _job_id = await _create_run(run_service, run_type=RunType.MATCH)  # seq 0
    await run_service.emit_status(run, status=RunStatus.RUNNING, message_key="m.running")
    await run_service.complete_run(run, reason="done")  # terminal

    # resolve_initial authorizes via job_id found in the MATCH run config snapshot.
    last = await svc.resolve_initial(_actor(), run.id, None)
    assert last == -1
    text = await _drain(svc.stream(_actor(), run.id, last), timeout=1.0)
    assert _sequences(text) == [0, 1, 2]
    assert "SSE_AUTH_REVOKED" not in text


def test_redis_subscription_wait_returns_within_timeout_when_pubsub_silent() -> None:
    """Regression for the e2e SSE hang (FIN-005).

    In the shared-Redis stack ``get_message(timeout=...)`` ignored its own timeout
    and blocked until a real cross-process message arrived; the worker's publish
    never reached the API SSE connection, so ``wait`` hung and the §13.2 PostgreSQL
    re-read never fired — the ranking stayed empty and recruitment-flow.spec.ts
    timed out with zero candidates. ``_RedisSubscription.wait`` now bounds the read
    with asyncio.wait_for, so it must return inside ``timeout`` even when the
    Pub/Sub wake is silent forever.
    """

    class _BlockingPubSub:
        async def subscribe(self, channel: str) -> None:  # pragma: no cover - exercised
            return None

        async def get_message(self, *, ignore_subscribe_messages: bool = False):
            # A notify that never arrives: block indefinitely to mimic the e2e hang.
            await asyncio.sleep(3600)

    class _FakeRedis:
        def pubsub(self) -> _BlockingPubSub:
            return _BlockingPubSub()

    sub = _RedisSubscription(_FakeRedis(), "run:abc")
    # wait must return inside the bound; the outer wait_for fails the test if it does not.
    asyncio.run(asyncio.wait_for(sub.wait(0.2), timeout=1.0))
