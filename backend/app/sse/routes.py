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

from collections.abc import AsyncGenerator
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


async def sse_service(request: Request) -> AsyncGenerator[SseService, None]:
    """Build the SSE service per request from process resources (yield = DI scope)."""
    resources: RuntimeResources = request.app.state.resources
    settings = request.app.state.settings
    async with resources.session_factory() as session:
        # Pass the factory, not a single session: the repository opens a fresh
        # session on every SSE poll so each replay read sees the worker's latest
        # commit (§13.2). The JobService session here is only used for the stable
        # job-access authorization check, so its snapshot does not matter.
        agent_repo = SqlAgentRunRepository(session_factory=resources.session_factory)
        job_service = JobService(session)

        async def authorize_job(actor: Actor, job_id: UUID) -> None:
            await job_service.get_authorized(actor, job_id)

        yield SseService(
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
