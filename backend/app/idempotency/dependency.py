"""FastAPI idempotency dependency (FIN-001).

``idempotency_guard`` is an async-generator dependency that:

1. Reads the ``Idempotency-Key`` header and hashes the full request
   (method + path + query + body).
2. Resolves it via :class:`IdempotencyService`. ``replay`` short-circuits with the
   frozen response; ``conflict`` rejects with 409; ``new`` yields a guard.
3. After the handler runs, if it never recorded success, the ``IN_PROGRESS`` lock
   is cleared so a retried request can re-run safely. Business operations are
   themselves idempotent, so a re-run cannot double-apply a side effect.

Endpoints opt in simply by adding ``guard: IdempotencyGuardDep`` and calling
``await guard.complete(status, body, resource_id=...)`` before returning. Requests
without the header pass through untouched (opt-in, no breaking change).
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, Request

from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.idempotency.errors import IdempotencyKeyReusedError, IdempotencyReplay
from backend.app.idempotency.repository import SqlIdempotencyRepository
from backend.app.idempotency.service import IdempotencyService

ActorDep = Annotated[Actor, Depends(get_current_actor)]


def _hash_request(request: Request, body: bytes) -> str:
    """Stable hash of the request identity (method|path|query|body)."""
    digest = hashlib.sha256()
    digest.update(request.method.encode("utf-8"))
    digest.update(b"|")
    digest.update(request.url.path.encode("utf-8"))
    digest.update(b"|")
    digest.update(request.url.query.encode("utf-8"))
    digest.update(b"|")
    digest.update(body)
    return digest.hexdigest()


class IdempotencyGuard:
    """Resolved guard handed to the route; records the successful response."""

    def __init__(
        self,
        *,
        service: IdempotencyService | None = None,
        key: str | None = None,
        operation: str = "",
        actor_id: object | None = None,
        request_hash: str = "",
        skip: bool = False,
    ) -> None:
        self.service = service
        self.key = key
        self.operation = operation
        self.actor_id = actor_id
        self.request_hash = request_hash
        self.skip = skip

    async def complete(
        self, status_code: int, body: dict[str, Any], resource_id: str | None = None
    ) -> None:
        """Freeze the response for future identical requests (FIN-001 replay)."""
        if self.skip or self.service is None or self.key is None:
            return
        await self.service.record_success(
            key=self.key, status_code=status_code, body=body, resource_id=resource_id
        )


async def _idempotency_dependency(
    request: Request, actor: ActorDep
) -> AsyncIterator[IdempotencyGuard]:
    key = request.headers.get("Idempotency-Key")
    if not key:
        # Opt-in: endpoints without the header are unaffected.
        yield IdempotencyGuard(skip=True)
        return

    body = await request.body()
    request_hash = _hash_request(request, body)
    route = request.scope.get("route")
    operation = route.path if route is not None else request.url.path
    resources = request.app.state.resources
    service = IdempotencyService(SqlIdempotencyRepository(resources.session_factory))

    outcome = await service.resolve(
        key=key, operation=operation, actor_id=actor.user_id, request_hash=request_hash
    )
    if outcome.kind == "replay":
        raise IdempotencyReplay(status=outcome.status or 200, body=outcome.body or {})
    if outcome.kind == "conflict":
        raise IdempotencyKeyReusedError()

    guard = IdempotencyGuard(
        service=service,
        key=key,
        operation=operation,
        actor_id=actor.user_id,
        request_hash=request_hash,
    )
    try:
        yield guard
    finally:
        # Only an IN_PROGRESS record is removed; a COMPLETED record (handler called
        # complete()) is left intact for replay. On a failed handler the lock is
        # cleared so a same-key retry can re-run without a stale block.
        await service.clear_in_progress(key)


IdempotencyGuardDep = Annotated[
    IdempotencyGuard, Depends(_idempotency_dependency, scope="function")
]
