"""OpenAI-compatible Qwen chat gateway (FIN-008).

``QwenChatGateway`` implements the same ``ModelGateway`` protocol as
``FakeModelGateway``, so nothing upstream of the boundary changes when the real
adapter is switched on. It speaks the OpenAI-compatible chat-completions shape
that the design selects (§12.2), which keeps the vendor swappable through
``model_base_url`` alone.

Two design points are worth stating because they are the ones most easily got
wrong:

**The model's reply is untrusted input, not a result.** It is parsed as JSON and
then validated against ``CandidateProfileDraft``. A reply that is not valid JSON,
or that carries a field the schema forbids, is a permanent failure — resending
the identical request would produce the identical malformed reply. This is why
the parse error is a ``ResponseSchemaError`` and not something the Celery layer
would retry.

**Credentials never leave the settings object.** The API key is read from a
``SecretStr`` at call time and placed into the request header only. It is never
logged, never embedded in a URL, and never included in the model snapshot that
the evaluation layer persists.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from backend.app.candidates.schemas import CandidateProfileDraft
from backend.app.core.settings import Settings
from backend.app.documents.parsers import ParsedBlock
from backend.app.infrastructure.chat_completion import (
    CHAT_COMPLETIONS_PATH,
    ChatCompletionShapeError,
    UsageRecord,
    extract_message_content,
)
from backend.app.infrastructure.http_transport import (
    JsonTransport,
    parse_json_object,
)
from backend.app.infrastructure.prompts import (
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_SYSTEM_PROMPT,
    build_extraction_user_prompt,
)

logger = logging.getLogger(__name__)

# Bounded prompt budget. The model snapshot records this, so a change here is a
# change in what an evaluation case actually measured.
MAX_PROMPT_CHARS = 24_000


def parse_structured_reply(content: str) -> CandidateProfileDraft:
    """Parse the assistant text into a validated draft.

    Tolerates a markdown-fenced reply because models add fences unbidden even
    when told not to, and stripping a fence is a lossless recovery — unlike
    guessing at a malformed object. Anything past that is a permanent schema
    failure.
    """
    text = content.strip()
    if text.startswith("```"):
        # Drop the opening fence line and the trailing fence if present.
        without_open = text.split("\n", 1)[1] if "\n" in text else ""
        text = without_open.rsplit("```", 1)[0] if "```" in without_open else without_open
        text = text.strip()

    try:
        decoded = json.loads(text)
    except ValueError as error:
        raise ChatCompletionShapeError(
            "model reply was not valid JSON", details={"length": len(text)}
        ) from error

    if not isinstance(decoded, dict):
        raise ChatCompletionShapeError("model reply was JSON but not an object")

    try:
        return CandidateProfileDraft.model_validate(decoded)
    except ValidationError as error:
        # A schema violation is permanent: the same prompt to the same model
        # would produce the same wrong shape. Retrying it would only burn the
        # retry budget a genuine transient failure needs, so it is translated
        # into the permanent transport error the Celery layer already understands.
        raise ChatCompletionShapeError(
            "model reply did not match the candidate profile schema",
            details={"errors": error.error_count()},
        ) from error


class QwenChatGateway:
    """Real chat gateway against an OpenAI-compatible ``/chat/completions``."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        transport: JsonTransport,
        timeout_seconds: float,
        temperature: float = 0.0,
    ) -> None:
        self.version = f"qwen-chat:{model}:{EXTRACTION_PROMPT_VERSION}"
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._transport = transport
        self._timeout = timeout_seconds
        # Extraction is a structured task: sampling only adds variance to a
        # result that is supposed to be reproducible.
        self._temperature = temperature
        # Accumulated so a caller can report what a run actually cost. The fake
        # has no counterpart: a stand-in that invented token counts would make
        # mock-mode cost figures look like measurements.
        self.usage = UsageRecord()

    @property
    def model(self) -> str:
        return self._model

    def _headers(self) -> dict[str, str]:
        # The key goes here and nowhere else. Never into a log, never into the
        # URL (a URL can end up in an access log on either side).
        return {"authorization": f"Bearer {self._api_key}"}

    async def extract_profile(
        self, *, full_text: str, blocks: Sequence[ParsedBlock]
    ) -> CandidateProfileDraft:
        """Send the resume text and return a schema-validated draft."""
        content = await self.complete_extraction_raw(full_text=full_text)
        draft = parse_structured_reply(content)
        logger.info(
            "qwen_extraction_completed",
            extra={
                "model": self._model,
                "prompt_version": EXTRACTION_PROMPT_VERSION,
                # Only the count is logged; the extracted values are candidate
                # personal data and never belong in a log line.
                "skill_count": len(draft.skills),
            },
        )
        return draft

    async def complete_extraction_raw(self, *, full_text: str) -> str:
        """Return the assistant text verbatim, without parsing it.

        Public because the recording layer needs the *raw* reply: a fixture that
        stored the parsed draft would bake this parser's current behaviour into
        the evidence and could not detect a later parser regression.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "temperature": self._temperature,
            "messages": [
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_extraction_user_prompt(
                        full_text=full_text, max_chars=MAX_PROMPT_CHARS
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
        }

        body = await self._transport.post_json(
            f"{self._base_url}{CHAT_COMPLETIONS_PATH}",
            payload,
            headers=self._headers(),
            timeout=self._timeout,
        )
        envelope = parse_json_object(body)
        # Recorded before the content is pulled out: a completion that arrived
        # with an unusable body was still billed, and a cost report that counted
        # only the calls that parsed would understate what was spent.
        self.usage.record(dict(envelope))
        return extract_message_content(dict(envelope))


def build_qwen_chat_gateway(
    settings: Settings, *, transport: JsonTransport | None = None
) -> QwenChatGateway:
    """Build the real chat gateway from settings.

    Raises ``ValueError`` when the endpoint or key is missing rather than
    silently degrading to the fake: a caller asking for the real adapter and
    receiving the heuristic one would produce plausible-looking but fabricated
    extractions, which is far worse than a loud startup failure. The settings
    validator already rejects a non-mock configuration without these values, so
    this is a defence in depth for direct construction.
    """
    if settings.model_base_url is None or settings.qwen_api_key is None:
        raise ValueError(
            "model_base_url and qwen_api_key are required to build the Qwen chat gateway"
        )
    sender_transport = transport
    if sender_transport is None:
        from backend.app.infrastructure.http_transport import HttpxJsonSender

        sender_transport = JsonTransport(HttpxJsonSender(), max_attempts=3)
    return QwenChatGateway(
        base_url=settings.model_base_url,
        api_key=settings.qwen_api_key.get_secret_value(),
        model=settings.chat_model,
        transport=sender_transport,
        timeout_seconds=float(settings.model_timeout_seconds),
    )


__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "MAX_PROMPT_CHARS",
    "ChatCompletionShapeError",
    "QwenChatGateway",
    "build_qwen_chat_gateway",
    "parse_structured_reply",
]
