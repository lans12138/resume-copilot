"""HTTP correlation and stable error-contract tests."""

from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as SqlTimeoutError

from backend.app.core.errors import AppError
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.main import create_app
from tests.unit.fake_resources import FakeRuntimeResources, make_fake_resources
from tests.unit.settings_factory import make_settings


def _client(application: FastAPI, *, raise_server_exceptions: bool = True) -> TestClient:
    return TestClient(application, raise_server_exceptions=raise_server_exceptions)


def test_server_generated_request_id_is_in_header_and_body() -> None:
    response = _client(create_app(make_settings())).get("/api/v1/health/live")

    request_id = response.headers["X-Request-ID"]
    assert request_id.startswith("req_")
    assert response.json() == {"status": "ok", "request_id": request_id}


def test_valid_upstream_request_id_is_preserved() -> None:
    response = _client(create_app(make_settings())).get(
        "/api/v1/health/live",
        headers={"X-Request-ID": "upstream-request-123"},
    )

    assert response.headers["X-Request-ID"] == "upstream-request-123"
    assert response.json()["request_id"] == "upstream-request-123"


def test_invalid_upstream_request_id_is_replaced() -> None:
    response = _client(create_app(make_settings())).get(
        "/api/v1/health/live",
        headers={"X-Request-ID": "bad request id"},
    )

    request_id = response.headers["X-Request-ID"]
    assert request_id.startswith("req_")
    assert "bad request" not in request_id


def test_app_error_uses_stable_public_contract() -> None:
    application = create_app(make_settings())

    @application.get("/api/v1/test/conflict")
    def conflict() -> None:
        raise AppError(
            code="VERSION_CONFLICT",
            http_status=409,
            safe_message="资源版本已变化",
            details={"current_version": 4},
            retryable=True,
        )

    response = _client(application).get(
        "/api/v1/test/conflict",
        headers={"X-Request-ID": "request-conflict-123"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "VERSION_CONFLICT",
        "message": "资源版本已变化",
        "request_id": "request-conflict-123",
        "details": {"current_version": 4},
    }


def test_validation_error_does_not_echo_invalid_input() -> None:
    application = create_app(make_settings())

    @application.get("/api/v1/test/validation")
    def validation(limit: int) -> dict[str, int]:
        return {"limit": limit}

    response = _client(application).get("/api/v1/test/validation?limit=private-value")

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert "private-value" not in response.text


def test_unknown_exception_returns_safe_500_without_internal_details() -> None:
    application = create_app(make_settings())

    @application.get("/api/v1/test/unexpected")
    def unexpected() -> None:
        raise RuntimeError("Bearer internal-sensitive-token")

    response = _client(application, raise_server_exceptions=False).get(
        "/api/v1/test/unexpected"
    )

    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert "internal-sensitive-token" not in response.text


def test_pool_exhaustion_is_a_retryable_503_not_a_fault() -> None:
    """A saturated connection pool is a dependency outage, not a bad request or a crash.

    Regression for CI run 35052716044: a handful of concurrent SSE streams pinned
    every pooled connection, after which *every* route (login included) failed with
    ``sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached``.
    It surfaced as a bare 500 after the full 30s ``DB_POOL_TIMEOUT``, which tells the
    operator nothing and tells the browser the request was at fault. The contract for
    an unavailable dependency is 503 + ``Retry-After``, so the caller can retry.
    """
    application = create_app(make_settings())

    @application.get("/api/v1/test/exhausted")
    def exhausted() -> None:
        raise SqlTimeoutError(
            "QueuePool limit of size 5 overflow 10 reached, connection timed out, "
            "timeout 30.00 (Background on this error at: https://sqlalche.me/e/20/3o7r)"
        )

    response = _client(application, raise_server_exceptions=False).get(
        "/api/v1/test/exhausted"
    )

    assert response.status_code == 503
    assert response.json()["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert response.headers["Retry-After"] == "1"
    # The internal driver text must not reach the client.
    assert "QueuePool" not in response.text


def test_readiness_endpoint_reports_all_dependencies() -> None:
    resources = make_fake_resources()
    application = create_app(make_settings(), resources_factory=lambda _settings: resources)

    with _client(application) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert response.json()["dependencies"]["postgres"] == {"status": "up"}


def test_readiness_endpoint_returns_safe_503() -> None:
    concrete_resources = FakeRuntimeResources("redis")
    resources = cast(RuntimeResources, concrete_resources)
    application = create_app(make_settings(), resources_factory=lambda _settings: resources)

    with _client(application) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.json()["details"]["dependencies"]["redis"] == {"status": "down"}
    assert "internal diagnostic" not in response.text
    assert concrete_resources.closed is True
