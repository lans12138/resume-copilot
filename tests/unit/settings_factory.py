"""Synthetic settings factory shared by unit tests."""

from pathlib import Path

from backend.app.core.settings import Settings


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "jwt_secret": "unit-test-secret-" + ("x" * 48),
        "database_url": "postgresql+asyncpg://test:test@postgres/test",
        "redis_url": "redis://redis:6379/0",
        "celery_broker_url": "redis://redis:6379/1",
        "storage_root": Path("/tmp/resume-copilot-tests"),
        "mock_model_mode": True,
        "log_format": "json",
    }
    values.update(overrides)
    return Settings.model_validate(values)
