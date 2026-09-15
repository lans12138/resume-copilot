"""Transactional SQL wiring for the evaluation endpoints (FIN-007).

Follows ``job_applications.wiring``: a per-request session, the service built over
it, and delivery published *after* commit. The ordering matters — publishing
before the commit would let a worker read a run that does not exist yet.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.evaluations.enqueuer import CeleryEvaluationEnqueuer, EvaluationEnqueuer
from backend.app.evaluations.repository import (
    SqlDatasetVersionRepository,
    SqlEvaluationRunRepository,
)
from backend.app.evaluations.service import EvaluationService
from backend.app.infrastructure.celery import app as celery_app
from backend.app.infrastructure.runtime import RuntimeResources

# The Celery binding for this service, so tests can swap in an in-memory fake.
CeleryTaskApp = celery_app


def build_evaluation_service(session: AsyncSession) -> EvaluationService:
    """Build the evaluation service over one database transaction."""
    return EvaluationService(
        datasets=SqlDatasetVersionRepository(session),
        runs=SqlEvaluationRunRepository(session),
    )


async def evaluation_service(
    request: Request,
) -> AsyncGenerator[EvaluationService, None]:
    """Yield an evaluation service and atomically commit all resulting facts."""
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        try:
            service = build_evaluation_service(session)
            yield service
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


def evaluation_enqueuer() -> EvaluationEnqueuer:
    """The production delivery port (Celery over the evaluations queue)."""
    return CeleryEvaluationEnqueuer(CeleryTaskApp)


__all__ = [
    "CeleryTaskApp",
    "build_evaluation_service",
    "evaluation_enqueuer",
    "evaluation_service",
]
