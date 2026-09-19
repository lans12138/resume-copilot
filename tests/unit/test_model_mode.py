"""Runtime model-mode endpoint and its view (PORT-005).

The banner exists so a viewer can tell a real model's answer from a scripted one
before drawing a conclusion from the screen. Two things are pinned here: the view
names the mode unambiguously, and the endpoint states it without leaking the API key
or the base URL — a provenance record must not become a place credentials reach the
browser.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.observability.model_mode import describe_model_mode
from tests.unit.settings_factory import make_settings

SECRET = "sentinel-key-do-not-echo"
ENDPOINT = "https://internal.example.invalid/v1"


def _settings(**overrides: object) -> Any:
    values: dict[str, object] = {
        "chat_model": "qwen3.7-plus",
        "embedding_model": "qwen3.7-text-embedding",
        "embedding_dimension": 1024,
        "qwen_api_key": SECRET,
        "model_base_url": ENDPOINT,
    }
    values.update(overrides)
    return make_settings(**values)


class TestDescribeModelMode:
    def test_names_mock_mode_unambiguously(self) -> None:
        """「Mock」 alone leaves a reader unable to say what it means for the answer."""
        view = describe_model_mode(_settings(mock_model_mode=True))

        assert view.mock_model_mode is True
        assert "Mock" in view.source_label
        assert "不调用外部服务" in view.source_label

    def test_names_the_real_mode(self) -> None:
        view = describe_model_mode(_settings(mock_model_mode=False))

        assert view.mock_model_mode is False
        assert view.source_label == "真实模型"

    def test_reports_the_configured_models(self) -> None:
        """The mode flag is not enough: the model names are what make a result attributable."""
        view = describe_model_mode(
            _settings(
                chat_model="custom-chat",
                embedding_model="custom-embed",
                embedding_dimension=768,
            )
        )

        assert view.chat_model == "custom-chat"
        assert view.embedding_model == "custom-embed"
        assert view.embedding_dimension == 768

    def test_carries_the_versions_that_shaped_a_result(self) -> None:
        view = describe_model_mode(_settings(prompt_version="v9", rule_version="v7"))

        assert view.prompt_version == "v9"
        assert view.rule_version == "v7"


class TestModelModeEndpoint:
    @pytest.fixture(scope="class")
    def client(self) -> TestClient:
        return TestClient(create_app(_settings(mock_model_mode=True)))

    def test_requires_authentication(self, client: TestClient) -> None:
        """A fact about the deployment, not a public one."""
        assert client.get("/api/v1/runtime/model-mode").status_code in (401, 403)

    def test_is_registered_in_the_openapi_document(self) -> None:
        paths = create_app(_settings()).openapi()["paths"]

        assert "/api/v1/runtime/model-mode" in paths
        assert "get" in paths["/api/v1/runtime/model-mode"]

    def test_never_echoes_the_api_key_or_the_endpoint(self) -> None:
        """The banner is rendered in a browser; the credentials must not travel."""
        schema = create_app(_settings()).openapi()["components"]["schemas"]["ModelModeOut"]
        properties = schema["properties"]

        assert "qwen_api_key" not in properties
        assert "model_base_url" not in properties
        assert "api_key" not in properties
