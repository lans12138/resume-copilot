"""SSE replay, continuous authorization, and live streaming (IMP-025, §13).

``SseService`` owns the contract the detailed design requires:

* **Replay** — ``GET .../events`` with a ``Last-Event-ID`` header replays every
  event with ``sequence > last_sequence`` in order, capped at ``batch_size``. An
  out-of-range or foreign cursor is rejected with ``INVALID_EVENT_CURSOR``/400
  (§13.3 step 2).
* **Continuous authorization** — every replay batch, every live batch, and every
  heartbeat re-checks run access. A revoked ``JobAssignment`` — or an actor that
  may no longer see the job at all — stops business events and closes the stream
  with ``SSE_AUTH_REVOKED`` (§13.4). Authorization is checked *before* reading
  sensitive events, never after. Only the access decisions (401/403/404) are
  reported as a revocation; any other failure propagates as itself, because
  publishing an outage as a permission change makes the browser drop a session
  that was never invalid.
* **Live loop** — after catching up, the stream awaits the notifier (Redis Pub/Sub
  in production, an in-process broadcast in tests) or a heartbeat. The notifier
  carries only the sequence; the loop re-reads PostgreSQL, so a lost notification
  is recovered by the next heartbeat (§13.2). The borrowed connection is released
  *before every ``yield``* and before the idle wait, so neither a suspended
  stream nor an idle one holds a pool slot.
* **Terminal close** — once the run reaches COMPLETED/FAILED/CANCELLED the stream
  emits a final heartbeat and closes (§13.3 step 6).

The service is deliberately free of FastAPI and SQLAlchemy Session concerns: it
takes an ``AgentRunRepository`` port, an ``authorize_job`` callback, and an
``EventNotifier``. The route injects the per-request session adapter and the
process-global notifier.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from uuid import UUID

from backend.app.agent.models import AgentRun
from backend.app.agent.repository import AgentRunRepository
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError, app_error
from backend.app.sse.notifier import EventNotifier
from backend.app.sse.schemas import (
    TERMINAL_STATUSES,
    format_auth_revoked,
    format_event,
    format_heartbeat,
    format_retry,
)

# authorize_job raises AppError (403/404) when the actor may not view the job.
AuthorizeJob = Callable[[Actor, UUID], Awaitable[None]]


class SseService:
    """Stream an AgentRun's event log over Server-Sent Events."""

    def __init__(
        self,
        agent_repo: AgentRunRepository,
        authorize_job: AuthorizeJob,
        notifier: EventNotifier,
        *,
        heartbeat_seconds: float,
        batch_size: int,
        retry_milliseconds: int,
    ) -> None:
        self._agent_repo = agent_repo
        self._authorize_job = authorize_job
        self._notifier = notifier
        self._heartbeat = float(heartbeat_seconds)
        self._batch = int(batch_size)
        self._retry_ms = int(retry_milliseconds)

    async def resolve_initial(self, actor: Actor, run_id: UUID, last_event_id: str | None) -> int:
        """Authenticate + authorize, then resolve the replay start sequence.

        Raises 404 when the run is unknown and 400 when ``Last-Event-ID`` is not a
        valid sequence belonging to this run. Must run *before* the first byte is
        streamed so the HTTP status can be set normally.
        """
        try:
            run = await self._agent_repo.get_run(run_id)
            if run is None:
                raise app_error("RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")
            await self._authorize_run(actor, run)
            cursor = await self._parse_cursor(run_id, last_event_id)
        except BaseException:
            # The reads above borrowed a connection from the pool. When this method
            # raises, ``stream`` never runs, so its ``finally`` — the only other
            # caller of ``aclose`` — never runs either, and the session would be
            # abandoned to the garbage collector still checked out (SQLAlchemy warns
            # "garbage collector is cleaning up non-checked-in connection"). A viewer
            # without an assignment getting a 403 is a normal answer, not a reason to
            # permanently cost the process a pool slot. ``shield`` so the release also
            # completes when the request was cancelled mid-resolution, and
            # ``suppress`` so this frame re-raises the original failure.
            with contextlib.suppress(BaseException):
                await asyncio.shield(self._agent_repo.aclose())
            raise
        # The handshake is over and the route only needs the cursor: release the
        # connection before the response starts. Otherwise a client that vanishes
        # in the gap between the headers and the first body chunk leaves the session
        # checked out against a stream that never begins — and because the generator
        # was never entered, no ``finally`` will ever clean it up.
        await self._agent_repo.refresh_for_poll()
        return cursor

    async def stream(
        self, actor: Actor, run_id: UUID, last_sequence: int
    ) -> AsyncGenerator[str, None]:
        """Yield SSE frames: replay batches, live updates, heartbeats, then close."""
        # A generator suspended at a ``yield`` cannot hand its pooled connection
        # back, so nothing below may park a connection across a frame. The handshake
        # already released its own; this covers a stream driven without it (tests,
        # or a caller that skipped ``resolve_initial``).
        await self._agent_repo.refresh_for_poll()
        yield format_retry(self._retry_ms)
        subscription = self._notifier.subscribe(run_id)
        try:
            while True:
                # Drop the frozen transaction/identity-map snapshot from the prior
                # iteration so this read sees data the worker committed since the
                # last poll (§13.2: the heartbeat re-read must observe the finish).
                await self._agent_repo.refresh_for_poll()

                # Continuous authorization before every batch (§13.4).
                try:
                    run = await self._agent_repo.get_run(run_id)
                    if run is None:
                        return  # run disappeared; close quietly
                    # Read every ORM attribute this iteration needs *before* the
                    # transaction ends. ``refresh_for_poll`` expires the identity map,
                    # so touching a lazy attribute after it would quietly re-open a
                    # transaction at the exact point the stream is about to yield.
                    terminal = run.status.value in TERMINAL_STATUSES
                    await self._authorize_run(actor, run)
                except AppError as error:
                    # Only a real access decision may be reported as one. A
                    # blanket ``except Exception`` used to turn a database outage
                    # into ``SSE_AUTH_REVOKED``, which the browser honours by
                    # dropping the session and bouncing the user to the login
                    # screen — i.e. an infrastructure failure was published as a
                    # permission change (CI run 35052716044). A dependency failure
                    # must stay one, so anything outside this set re-raises.
                    #
                    # 404 belongs to the set because it is how
                    # ``JobService.get_authorized`` says "you may not see this
                    # job": ``JOB_NOT_FOUND`` hides existence instead of confirming
                    # it, and §12 allows 403 *or* 404 for the events route. Letting
                    # that escape would tear the body down mid-stream, which the
                    # browser cannot distinguish from a dropped connection — it
                    # would reconnect, be refused 404 again, and retry forever. The
                    # frame is what turns the closure into an answer.
                    if error.http_status not in (401, 403, 404):
                        raise
                    await self._agent_repo.refresh_for_poll()
                    yield format_auth_revoked()
                    return

                events = await self._agent_repo.list_events_after(
                    run_id, last_sequence, self._batch
                )
                # Render the frames while the transaction is still the one that read
                # them, then end that transaction. The rows are plain data and
                # ``format_event`` does no IO, so the finished strings outlive the
                # rollback; yielding the ORM events instead would touch expired
                # attributes after it and pull the connection straight back.
                frames = [format_event(event) for event in events]
                if events:
                    last_sequence = events[-1].sequence

                # Release the borrowed connection before yielding *and* before going
                # idle. Ending the transaction is what actually hands the pooled
                # connection back, and a suspended generator cannot do it: a browser
                # that stalls mid-frame, navigates away, or is closed while a
                # reconnect is pending parked the connection for good. Fourteen such
                # streams sat in ``idle in transaction`` — every one of them on this
                # very events SELECT — filled the 5+10 pool, and turned every
                # unrelated request, login included, into a 30s wait followed by a 503
                # once ``DB_POOL_TIMEOUT`` expired (web probe run 2, 2026-09-16).
                await self._agent_repo.refresh_for_poll()

                for frame in frames:
                    yield frame

                if terminal:
                    yield format_heartbeat()
                    return

                # Wait for a notify wake-up or the heartbeat deadline. A lost
                # notify is harmless: the next heartbeat re-reads PostgreSQL.
                #
                # ``wait`` reports *which* of the two happened rather than raising,
                # because the keep-alive frame is the observable proof that the
                # stream is being forwarded incrementally (a buffering proxy would
                # hold it) and therefore has to be emitted on every deadline.
                woke = await subscription.wait(self._heartbeat)
                if not woke:
                    yield format_heartbeat()
        finally:
            await subscription.aclose()
            await self._agent_repo.aclose()

    async def _authorize_run(self, actor: Actor, run: AgentRun) -> None:
        job_id = self._extract_job_id(run)
        await self._authorize_job(actor, job_id)

    @staticmethod
    def _extract_job_id(run: AgentRun) -> UUID:
        raw = run.config_snapshot_json.get("job_id") if run.config_snapshot_json else None
        if not raw:
            raise app_error(
                "RUN_JOB_UNRESOLVABLE",
                http_status=500,
                safe_message="无法从流程解析所属岗位",
            )
        try:
            return UUID(str(raw))
        except ValueError as error:
            raise app_error(
                "RUN_JOB_UNRESOLVABLE",
                http_status=500,
                safe_message="流程所属岗位标识非法",
            ) from error

    async def _parse_cursor(self, run_id: UUID, last_event_id: str | None) -> int:
        if not last_event_id:
            return -1  # replay from the beginning
        try:
            sequence = int(last_event_id)
        except ValueError as error:
            raise app_error(
                "INVALID_EVENT_CURSOR",
                http_status=400,
                safe_message="Last-Event-ID 不是合法序号",
            ) from error
        if sequence < 0:
            raise app_error(
                "INVALID_EVENT_CURSOR",
                http_status=400,
                safe_message="Last-Event-ID 序号非法",
            )
        existing = await self._agent_repo.get_event_by_sequence(run_id, sequence)
        if existing is None:
            raise app_error(
                "INVALID_EVENT_CURSOR",
                http_status=400,
                safe_message="Last-Event-ID 不属于该流程",
            )
        return sequence
