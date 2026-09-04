"""Synthetic settings factory shared by unit tests."""

import tempfile
from pathlib import Path

from backend.app.core.settings import Settings


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "jwt_secret": "unit-test-secret-" + ("x" * 48),
        "database_url": "postgresql+asyncpg://test:test@postgres/test",
        "redis_url": "redis://redis:6379/0",
        "celery_broker_url": "redis://redis:6379/1",
        "storage_root": Path(tempfile.gettempdir()) / "resume-copilot-tests",
        "mock_model_mode": True,
        "log_format": "json",
    }
    values.update(overrides)
    # `_env_file=None` keeps unit tests independent of the local project .env so
    # required-field and credential-gate assertions behave the same in CI and locally.
    return Settings(_env_file=None, **values)
