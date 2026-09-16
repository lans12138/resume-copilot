"""Stable application errors and FastAPI exception mapping."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import JsonValue
from sqlalchemy.exc import TimeoutError as SqlTimeoutError

from backend.app.core.context import get_request_id

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Expected application failure with a stable public contract."""

    def __init__(
        self,
        *,
        code: str,
        http_status: int,
        safe_message: str,
        details: dict[str, JsonValue] | None = None,
        retryable: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.code = code
        self.http_status = http_status
        self.safe_message = safe_message
        self.details = details or {}
        self.retryable = retryable
        self.headers = headers or {}


def _error_payload(
    *, code: str, message: str, details: dict[str, JsonValue] | None = None
) -> dict[str, JsonValue]:
    return {
        "code": code,
        "message": message,
        "request_id": get_request_id(),
        "details": details or {},
    }


async def handle_app_error(_request: Request, error: AppError) -> JSONResponse:
    """Map expected domain/application failures without exposing internals."""
    logger.warning(
        "application_error",
        extra={"error_code": error.code, "retryable": error.retryable},
    )
    return JSONResponse(
        status_code=error.http_status,
        headers=error.headers,
        content=_error_payload(
            code=error.code,
            message=error.safe_message,
            details=error.details,
        ),
    )


async def handle_validation_error(
    _request: Request, error: RequestValidationError
) -> JSONResponse:
    """Return bounded validation metadata without echoing submitted values."""
    safe_errors: list[JsonValue] = []
    for item in error.errors():
        safe_errors.append(
            {
                "field": ".".join(str(segment) for segment in item.get("loc", ())),
                "type": str(item.get("type", "validation_error")),
                "message": str(item.get("msg", "Invalid value")),
            }
        )

    logger.info("request_validation_failed", extra={"error_code": "VALIDATION_ERROR"})
    return JSONResponse(
        status_code=422,
        content=_error_payload(
            code="VALIDATION_ERROR",
            message="请求参数校验失败",
            details={"errors": safe_errors},
        ),
    )


async def handle_unexpected_error(_request: Request, error: Exception) -> JSONResponse:
    """Log unknown failures with a stack and return only a stable safe response."""
    logger.exception(
        "unhandled_exception",
        exc_info=error,
        extra={"error_code": "INTERNAL_ERROR"},
    )
    return JSONResponse(
        status_code=500,
        content=_error_payload(
            code="INTERNAL_ERROR",
            message="服务暂时不可用，请稍后重试",
        ),
    )


async def handle_pool_exhausted(_request: Request, error: Exception) -> JSONResponse:
    """Report a saturated database pool as a retryable dependency failure, not a fault.

    ``sqlalchemy.exc.TimeoutError`` means every pooled connection is checked out and
    the waiter hit ``DB_POOL_TIMEOUT`` (30s by default). Unhandled it surfaces as a
    bare 500 *after* that whole window, which is indistinguishable from a crash for
    both the operator and the browser, and the 30 seconds of silence apply to every
    route at once — login included. That is exactly what a handful of concurrent SSE
    streams produced in CI run 35052716044. A dependency outage is a 503 with
    ``Retry-After``; the caller may retry, and the UI must not treat it as bad input.
    """
    logger.error("database_pool_exhausted", extra={"error_code": "DEPENDENCY_UNAVAILABLE"})
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "1"},
        content=_error_payload(
            code="DEPENDENCY_UNAVAILABLE",
            message="数据库连接繁忙，请稍后重试",
        ),
    )


def register_exception_handlers(application: FastAPI) -> None:
    """Register the shared API exception contract."""
    application.add_exception_handler(AppError, handle_app_error)  # type: ignore[arg-type]
    application.add_exception_handler(RequestValidationError, handle_validation_error)  # type: ignore[arg-type]
    application.add_exception_handler(SqlTimeoutError, handle_pool_exhausted)  # type: ignore[arg-type]
    application.add_exception_handler(Exception, handle_unexpected_error)


def app_error(
    code: str,
    http_status: int,
    safe_message: str,
    *,
    details: dict[str, JsonValue] | None = None,
    retryable: bool = False,
    headers: dict[str, str] | None = None,
) -> AppError:
    """Small convenience factory for application services and policies."""
    return AppError(
        code=code,
        http_status=http_status,
        safe_message=safe_message,
        details=details,
        retryable=retryable,
        headers=headers,
    )
