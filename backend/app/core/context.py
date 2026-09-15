"""Request-scoped diagnostic context for logs and response correlation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Diagnostic request metadata; never used as the sole authorization source."""

    request_id: str
    actor_id: str | None = None
    role: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None


_request_context: ContextVar[RequestContext | None] = ContextVar(
    "request_context",
    default=None,
)


def get_request_context() -> RequestContext | None:
    """Return the current request context when one is bound."""
    return _request_context.get()


def get_request_id() -> str:
    """Return the current request ID or a safe fallback outside HTTP execution."""
    context = get_request_context()
    return context.request_id if context is not None else "unavailable"


def set_request_context(context: RequestContext) -> Token[RequestContext | None]:
    """Bind request context and return the token required for reset."""
    return _request_context.set(context)


def reset_request_context(token: Token[RequestContext | None]) -> None:
    """Restore the previous request context."""
    _request_context.reset(token)


@contextmanager
def request_context_scope(context: RequestContext) -> Iterator[None]:
    """Bind a context for the duration of a synchronous operation or test."""
    token = set_request_context(context)
    try:
        yield
    finally:
        reset_request_context(token)


@dataclass(frozen=True, slots=True)
class RunContext:
    """Run/operation diagnostic context for logs (§18.1). Not an authorization source."""

    run_id: str | None = None
    run_type: str | None = None
    thread_id: str | None = None
    attempt: int | None = None
    node: str | None = None
    operation_key: str | None = None
    task_id: str | None = None
    job_id: str | None = None
    application_id: str | None = None
    error_code: str | None = None
    duration_ms: float | None = None


_run_context: ContextVar[RunContext | None] = ContextVar(
    "run_context",
    default=None,
)


def get_run_context() -> RunContext | None:
    """Return the current run/operation context when one is bound."""
    return _run_context.get()


def set_run_context(context: RunContext) -> Token[RunContext | None]:
    """Bind run context and return the token required for reset."""
    return _run_context.set(context)


def reset_run_context(token: Token[RunContext | None]) -> None:
    """Restore the previous run context."""
    _run_context.reset(token)


@contextmanager
def run_context_scope(context: RunContext) -> Iterator[None]:
    """Bind a run context for the duration of a synchronous operation or test."""
    token = set_run_context(context)
    try:
        yield
    finally:
        reset_run_context(token)
