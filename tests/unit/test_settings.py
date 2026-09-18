"""Settings validation and secret-boundary tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.core.settings import AppEnvironment, Settings
from tests.unit.settings_factory import make_settings


def test_settings_accept_explicit_test_configuration() -> None:
    settings = make_settings()

    assert settings.app_env is AppEnvironment.TEST
    assert settings.embedding_dimension == 1024
    assert settings.mock_model_mode is True


def test_settings_ignore_a_local_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout's ``.env`` must not change what the configuration tests prove.

    ``Settings`` resolves ``env_file`` relative to the working directory, so
    without session-wide isolation a developer's local overrides would silently
    rewrite the expectations below. This test is the regression cover for that:
    it plants a ``.env`` in the working directory and asserts the real defaults
    still win (PORT-001).
    """
    (tmp_path / ".env").write_text(
        "CHAT_MODEL=leaked-from-dotenv\nMOCK_MODEL_MODE=false\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JWT_SECRET", "x" * 48)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@postgres/test")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://redis:6379/1")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))

    settings = Settings()

    assert settings.chat_model == "qwen3.7-plus"
    assert settings.mock_model_mode is True


@pytest.mark.parametrize(
    "field_name",
    ["model_base_url", "qwen_api_key", "langfuse_host", "langfuse_secret"],
)
def test_settings_treat_blank_optional_values_as_unset(field_name: str) -> None:
    """``MODEL_BASE_URL=`` means "not configured", not "configured but empty".

    Compose forwards these with an empty default, so a blank value has to land as
    ``None``. Otherwise it would pass the presence check and only fail at call
    time, with an empty endpoint in the error message.
    """
    settings = make_settings(**{field_name: ""})

    assert getattr(settings, field_name) is None


def test_settings_reject_blank_credentials_when_fake_model_is_disabled() -> None:
    with pytest.raises(ValidationError, match="model_base_url and qwen_api_key"):
        make_settings(mock_model_mode=False, model_base_url="", qwen_api_key="")


def test_settings_report_blank_required_values_as_missing() -> None:
    with pytest.raises(ValidationError, match="missing required configuration"):
        make_settings(storage_root="")


def test_settings_accept_a_blank_celery_result_backend() -> None:
    """``CELERY_RESULT_BACKEND`` is optional and forwarded with an empty default.

    An empty string is not a redis:// URL, so without blank-to-None normalization
    the container would refuse to start on a configuration that is actually fine.
    """
    settings = make_settings(celery_result_backend="")

    assert settings.celery_result_backend is None


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
