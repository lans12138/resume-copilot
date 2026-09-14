"""SSE replay, continuous authorization, and live streaming (IMP-025, §13).

``SseService`` owns the contract the detailed design requires:

* **Replay** — ``GET .../events`` with a ``Last-Event-ID`` header replays every
  event with ``sequence > last_sequence`` in order, capped at ``batch_size``. An
  out-of-range or foreign cursor is rejected with ``INVALID_EVENT_CURSOR``/400
  (§13.3 step 2).
* **Continuous authorization** — every replay batch, every live batch, and every
  heartbeat re-checks run access. A revoked ``JobAssignment`` (or disabled user)
  stops business events and closes the stream with ``SSE_AUTH_REVOKED`` (§13.4).
  Authorization is checked *before* reading sensitive events, never after.
* **Live loop** — after catching up, the stream awaits the notifier (Redis Pub/Sub
  in production, an in-process broadcast in tests) or a heartbeat. The notifier
  carries only the sequence; the loop re-reads PostgreSQL, so a lost notification
  is recovered by the next heartbeat (§13.2).
* **Terminal close** — once the run reaches COMPLETED/FAILED/CANCELLED the stream
  emits a final heartbeat and closes (§13.3 step 6).

The service is deliberately free of FastAPI and SQLAlchemy Session concerns: it
takes an ``AgentRunRepository`` port, an ``authorize_job`` callback, and an
``EventNotifier``. The route injects the per-request session adapter and the
process-global notifier.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from uuid import UUID

from backend.app.agent.models import AgentRun
from backend.app.agent.repository import AgentRunRepository
from backend.app.auth.tokens import Actor
from backend.app.core.errors import app_error
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
        run = await self._agent_repo.get_run(run_id)
        if run is None:
            raise app_error("RUN_NOT_FOUND", http_status=404, safe_message="流程不存在")
        await self._authorize_run(actor, run)
        return await self._parse_cursor(run_id, last_event_id)

    async def stream(
        self, actor: Actor, run_id: UUID, last_sequence: int
    ) -> AsyncGenerator[str, None]:
        """Yield SSE frames: replay batches, live updates, heartbeats, then close."""
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
                    await self._authorize_run(actor, run)
                except Exception:
                    yield format_auth_revoked()
                    return

                events = await self._agent_repo.list_events_after(
                    run_id, last_sequence, self._batch
                )
                for event in events:
                    yield format_event(event)
                    last_sequence = event.sequence

                if run.status.value in TERMINAL_STATUSES:
                    yield format_heartbeat()
                    return

                # Wait for a notify wake-up or the heartbeat deadline. A lost
                # notify is harmless: the next heartbeat re-reads PostgreSQL.
                try:
                    await subscription.wait(self._heartbeat)
                except TimeoutError:
                    yield format_heartbeat()
        finally:
            await subscription.aclose()

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
