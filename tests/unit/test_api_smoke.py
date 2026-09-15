"""Minimal API smoke tests for the IMP-001 skeleton."""

from fastapi.testclient import TestClient

from backend.app.main import create_app
from tests.unit.settings_factory import make_settings


def test_liveness_endpoint() -> None:
    app = create_app(make_settings())
    response = TestClient(app).get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_openapi_document_identifies_the_service() -> None:
    app = create_app(make_settings())
    response = TestClient(app).get("/api/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"] == {
        "title": "Resume Copilot API",
        "version": "0.1.0",
    }
