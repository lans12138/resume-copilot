"""FastAPI application factory shared by all API process entry points."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import JsonValue

from backend.app.approvals.routes import router as approvals_router
from backend.app.auth.routes import router as auth_router
from backend.app.candidates.routes import router as candidates_router
from backend.app.core.context import get_request_id
from backend.app.core.errors import AppError, register_exception_handlers
from backend.app.core.health import check_readiness
from backend.app.core.logging import configure_logging
from backend.app.core.middleware import RequestContextMiddleware
from backend.app.core.settings import Settings, get_settings
from backend.app.documents.routes import router as documents_router
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.job_applications.routes import router as application_runs_router
from backend.app.jobs.routes import router as jobs_router
from backend.app.reports.routes import router as reports_router


def create_app(
    settings: Settings | None = None,
    *,
    resources_factory: Callable[[Settings], RuntimeResources] = RuntimeResources.build,
) -> FastAPI:
    """Create the API and fail fast when process configuration is invalid."""
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resources = resources_factory(resolved_settings)
        application.state.resources = resources
        try:
            yield
        finally:
            await resources.close()

    application = FastAPI(
        title="Resume Copilot API",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.add_middleware(
        RequestContextMiddleware,
        request_id_header=resolved_settings.request_id_header,
    )
    register_exception_handlers(application)
    application.include_router(auth_router)
    application.include_router(jobs_router)
    application.include_router(documents_router)
    application.include_router(candidates_router)
    application.include_router(reports_router)
    application.include_router(application_runs_router)
    application.include_router(approvals_router)

    @application.get("/api/v1/health/live", tags=["health"])
    def liveness() -> dict[str, str]:
        return {"status": "ok", "request_id": get_request_id()}

    @application.get("/api/v1/health/ready", tags=["health"])
    async def readiness(request: Request) -> dict[str, JsonValue]:
        resources: RuntimeResources = request.app.state.resources
        ready, dependencies = await check_readiness(resources)
        if not ready:
            raise AppError(
                code="DEPENDENCY_UNAVAILABLE",
                http_status=503,
                safe_message="核心依赖尚未就绪",
                details={"dependencies": dependencies},
                retryable=True,
            )
        return {
            "status": "ready",
            "request_id": get_request_id(),
            "dependencies": dependencies,
        }

    return application
