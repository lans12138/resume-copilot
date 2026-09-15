"""Settings validation and secret-boundary tests."""

import pytest
from pydantic import ValidationError

from backend.app.core.settings import AppEnvironment, Settings
from tests.unit.settings_factory import make_settings


def test_settings_accept_explicit_test_configuration() -> None:
    settings = make_settings()

    assert settings.app_env is AppEnvironment.TEST
    assert settings.embedding_dimension == 1024
    assert settings.mock_model_mode is True


def test_settings_report_missing_required_field_names() -> None:
    with pytest.raises(ValidationError) as captured:
        Settings.model_validate({"app_env": "test"})

    message = str(captured.value)
    assert "jwt_secret" in message
    assert "database_url" in message
    assert "redis_url" in message
    assert "celery_broker_url" in message
    assert "storage_root" in message


def test_settings_require_model_credentials_when_fake_model_is_disabled() -> None:
    with pytest.raises(ValidationError, match="model_base_url and qwen_api_key"):
        make_settings(mock_model_mode=False)


def test_settings_require_at_least_one_retrieval_weight() -> None:
    with pytest.raises(ValidationError, match="retrieval weight"):
        make_settings(structured_weight=0, keyword_weight=0, vector_weight=0)


def test_settings_hide_secret_values_from_repr() -> None:
    secret_marker = "unit-test-secret-marker-" + ("z" * 48)
    settings = make_settings(jwt_secret=secret_marker)

    assert secret_marker not in repr(settings)
    assert "**********" in repr(settings)


@pytest.mark.parametrize(
    ("field_name", "value", "expected_message"),
    [
        ("database_url", "sqlite:///local.db", r"postgresql\+asyncpg"),
        ("redis_url", "http://redis:6379", "redis://"),
        ("storage_root", "relative/path", "absolute path"),
    ],
)
def test_settings_reject_invalid_infrastructure_contracts(
    field_name: str,
    value: str,
    expected_message: str,
) -> None:
    with pytest.raises(ValidationError, match=expected_message):
        make_settings(**{field_name: value})
