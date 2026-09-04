"""SSE wire-format helpers (IMP-025, detailed design §13.1/§13.3).

Each helper returns a fully-formed SSE block ending in a blank line. The client
keys off ``event`` / ``id`` / typed ``data`` only; ``id`` is the event sequence so
a reconnecting browser sends it back as ``Last-Event-ID`` for lossless replay.
"""

from __future__ import annotations

import json

from backend.app.agent.models import AgentEvent

# Run lifecycle states that end the stream (detailed design §13.3 step 6).
TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

SSE_AUTH_REVOKED = "SSE_AUTH_REVOKED"


def format_retry(retry_milliseconds: int) -> str:
    """Browser reconnect delay (SSE ``retry:`` directive), sent first."""
    return f"retry: {retry_milliseconds}\n\n"


def format_event(event: AgentEvent) -> str:
    """One ``event:/id:/data:`` block for a persisted AgentEvent."""
    payload = json.dumps(event.to_sse_dict(), ensure_ascii=False)
    return (
        f"event: {event.event_type.value}\n"
        f"id: {event.sequence}\n"
        f"data: {payload}\n\n"
    )


def format_heartbeat() -> str:
    """Keep-alive comment line; also the final frame before a terminal close."""
    return ":\n\n"


def format_auth_revoked() -> str:
    """Final frame sent when continuous authorization fails mid-stream."""
    return f"event: {SSE_AUTH_REVOKED}\ndata: {{\"reason\":\"access_revoked\"}}\n\n"
