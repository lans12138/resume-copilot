"""Which model answered, stated once (PORT-005).

The demo has to say out loud whether it is running against a real model or the
deterministic fake, because the two prove different things: a recorded evaluation is
only attributable if the reader knows which adapter produced it (§18.2). Nothing in
the UI said so — a viewer had no way to tell a real answer from a scripted one.

This module is the single source of that fact. ``mock_model_mode`` alone is not
enough: "Mock" without the model names leaves a reader unable to tell whether the
deployment merely has the fake enabled or is genuinely configured for an upstream it
has not reached. The API key and base URL are deliberately excluded — a provenance
record must not become a place credentials leak into the browser.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.core.settings import Settings


@dataclass(frozen=True, slots=True)
class ModelModeView:
    """The deployment's model configuration, as the UI must state it."""

    mock_model_mode: bool
    chat_model: str
    embedding_model: str
    embedding_dimension: int
    prompt_version: str
    rule_version: str

    @property
    def source_label(self) -> str:
        """One phrase naming where results come from, safe to show a viewer."""
        return "Mock 模型（确定性假模型，不调用外部服务）" if self.mock_model_mode else "真实模型"


def describe_model_mode(settings: Settings) -> ModelModeView:
    return ModelModeView(
        mock_model_mode=settings.mock_model_mode,
        chat_model=settings.chat_model,
        embedding_model=settings.embedding_model,
        embedding_dimension=settings.embedding_dimension,
        prompt_version=settings.prompt_version,
        rule_version=settings.rule_version,
    )
