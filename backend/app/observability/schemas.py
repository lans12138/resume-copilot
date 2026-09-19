"""Response schemas for the runtime introspection endpoint (PORT-005)."""

from __future__ import annotations

from pydantic import BaseModel

from backend.app.observability.model_mode import ModelModeView


class ModelModeOut(BaseModel):
    """The deployment's model configuration, for the on-screen provenance banner.

    Carries model *names* and the mode flag, never the API key or the base URL: the
    banner has to make results attributable without becoming a place credentials leak
    into the browser.
    """

    mock_model_mode: bool
    source_label: str
    chat_model: str
    embedding_model: str
    embedding_dimension: int
    prompt_version: str
    rule_version: str

    @classmethod
    def from_view(cls, view: ModelModeView) -> ModelModeOut:
        return cls(
            mock_model_mode=view.mock_model_mode,
            source_label=view.source_label,
            chat_model=view.chat_model,
            embedding_model=view.embedding_model,
            embedding_dimension=view.embedding_dimension,
            prompt_version=view.prompt_version,
            rule_version=view.rule_version,
        )
