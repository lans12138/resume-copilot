"""SSE event-stream endpoints (IMP-025, detailed design §12 / §13).

Two read-only streams — one per run aggregate — let the browser follow run
progress with continuous authorization and lossless reconnect:

* ``GET /application-runs/{id}/events``
* ``GET /match-runs/{id}/events``

Both carry a ``Last-Event-ID`` header for replay. Authorization is the same
job-level access check used everywhere else (§13.4): an unknown run is 404, and a
viewer without an active ``JobAssignment`` is 403. Mid-stream revocation closes
the connection with an ``SSE_AUTH_REVOKED`` frame rather than leaking events.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from backend.app.agent.repository import SqlAgentRunRepository
from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.jobs.service import JobService
from backend.app.sse.service import SseService

router = APIRouter(prefix="/api/v1", tags=["sse"])


async def sse_service(request: Request) -> SseService:
    """Build the SSE service per request from process resources.

    Deliberately *not* a ``yield`` dependency and deliberately not scoped to a
    request session: an SSE stream outlives every ordinary request, and a session
    opened here is only closed when the response finishes — i.e. when the run ends
    or the browser goes away. That pinned one pooled connection for the whole life
    of every live stream, on top of the one the repository holds, so eight
    concurrent streams exhausted the 5+10 pool and every request in the process
    (login included) then answered 500 after ``DB_POOL_TIMEOUT`` (CI run
    35052716044). Both dependencies below own short-lived sessions instead.
    """
    resources: RuntimeResources = request.app.state.resources
    settings = request.app.state.settings
    # Pass the factory, not a single session: the repository keeps one session for
    # the whole stream and releases its frozen READ COMMITTED snapshot (rollback +
    # expire_all) on every poll, so each replay read sees the worker's latest commit
    # without holding the connection across the idle wait (§13.2).
    agent_repo = SqlAgentRunRepository(session_factory=resources.session_factory)

    async def authorize_job(actor: Actor, job_id: UUID) -> None:
        # One session per check, not one per stream: the job-access check runs on
        # every replay batch, every live batch and every heartbeat (§13.4), so it is
        # a short read that must not carry a connection into the idle window.
        async with resources.session_factory() as session:
            await JobService(session).get_authorized(actor, job_id)

    return SseService(
        agent_repo,
        authorize_job,
        resources.event_notifier,
        heartbeat_seconds=settings.sse_heartbeat_seconds,
        batch_size=settings.sse_batch_size,
        retry_milliseconds=settings.sse_retry_milliseconds,
    )


ServiceDep = Annotated[SseService, Depends(sse_service)]
ActorDep = Annotated[Actor, Depends(get_current_actor)]


@router.get("/application-runs/{run_id}/events")
async def application_run_events(
    run_id: UUID,
    actor: ActorDep,
    service: ServiceDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Stream an ApplicationRun's events with replay + continuous auth."""
    last_sequence = await service.resolve_initial(actor, run_id, last_event_id)
    return StreamingResponse(
        service.stream(actor, run_id, last_sequence),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/match-runs/{run_id}/events")
async def match_run_events(
    run_id: UUID,
    actor: ActorDep,
    service: ServiceDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Stream a MatchRun's events with replay + continuous auth."""
    last_sequence = await service.resolve_initial(actor, run_id, last_event_id)
    return StreamingResponse(
        service.stream(actor, run_id, last_sequence),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
