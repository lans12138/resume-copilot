"""Real Qwen match-explanation adapter (PORT-003).

Mirrors ``qwen_chat``: same OpenAI-compatible envelope, same transport, same
"the reply is untrusted input" stance. The only difference is the schema it
validates against — a bounded, citation-bearing explanation instead of a profile
draft.

Two things this adapter does *not* do, both on purpose:

* It does not repair a reply. A missing citation is not filled in, a ``HIGH``
  impact is not clamped, a quote is not normalised to match the chunk. Each of
  those would turn a detectable contract violation into a plausible-looking
  explanation, which is precisely the failure mode the schema exists to prevent.
* It does not decide what a citation means. Verification happens in
  ``explanations.service``, against the candidate's own chunks, after the model
  has finished. Keeping the check out of the adapter means the same check applies
  to a replayed recording and to a hand-built test double.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from pydantic import ValidationError

from backend.app.core.settings import Settings
from backend.app.explanations.contract import MatchExplanationDraft
from backend.app.explanations.gateway import (
    MAX_EXPLANATION_PROMPT_CHARS,
    ExplanationCall,
    ExplanationRequest,
)
from backend.app.infrastructure.chat_completion import (
    CHAT_COMPLETIONS_PATH,
    ChatCompletionShapeError,
    extract_message_content,
    extract_usage,
)
from backend.app.infrastructure.http_transport import (
    AttemptRecord,
    JsonTransport,
    parse_json_object,
)
from backend.app.infrastructure.prompts import (
    MATCH_EXPLANATION_PROMPT_VERSION,
    MATCH_EXPLANATION_SYSTEM_PROMPT,
    build_match_explanation_user_prompt,
)

logger = logging.getLogger(__name__)


def parse_explanation_reply(content: str) -> MatchExplanationDraft:
    """Parse the assistant text into a validated explanation draft.

    Tolerates a markdown-fenced reply for the same reason the extraction parser
    does — models add fences unbidden even when told not to, and stripping one is
    lossless. Everything else is a permanent schema failure: a reply that is not
    JSON, or that carries a field the schema forbids (``HIGH`` impact, an invented
    key), will be identical if resent.
    """
    text = content.strip()
    if text.startswith("```"):
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
        return MatchExplanationDraft.model_validate(decoded)
    except ValidationError as error:
        # Includes the impact-escalation case: a conclusion asking for HIGH is
        # rejected here rather than clamped (contract.py explains why).
        raise ChatCompletionShapeError(
            "model reply did not match the match-explanation schema",
            details={"errors": error.error_count()},
        ) from error


class QwenExplanationGateway:
    """Real explanation gateway against an OpenAI-compatible ``/chat/completions``."""

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
        self.version = f"qwen-explain:{model}:{MATCH_EXPLANATION_PROMPT_VERSION}"
        self.prompt_version = MATCH_EXPLANATION_PROMPT_VERSION
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._transport = transport
        self._timeout = timeout_seconds
        self._temperature = temperature

    @property
    def model(self) -> str:
        return self._model

    def _headers(self) -> dict[str, str]:
        # The key goes here and nowhere else: not into a log, not into the URL,
        # not into the model snapshot the report persists.
        return {"authorization": f"Bearer {self._api_key}"}

    def _payload(self, request: ExplanationRequest) -> dict[str, Any]:
        return {
            "model": self._model,
            "temperature": self._temperature,
            "messages": [
                {"role": "system", "content": MATCH_EXPLANATION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_match_explanation_user_prompt(
                        job_text=request.job_text,
                        verdict_text=request.verdict_text,
                        evidence=tuple(
                            (str(item.chunk_id), item.text) for item in request.evidence
                        ),
                        max_chars=MAX_EXPLANATION_PROMPT_CHARS,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
        }

    async def explain(self, request: ExplanationRequest) -> ExplanationCall:
        started = time.perf_counter()
        attempts = AttemptRecord()
        body = await self._transport.post_json(
            f"{self._base_url}{CHAT_COMPLETIONS_PATH}",
            self._payload(request),
            headers=self._headers(),
            timeout=self._timeout,
            attempts=attempts,
        )
        envelope = dict(parse_json_object(body))
        content = extract_message_content(envelope)
        prompt_tokens, completion_tokens = extract_usage(envelope)
        draft = parse_explanation_reply(content)
        latency_ms = int((time.perf_counter() - started) * 1000)

        logger.info(
            "qwen_explanation_completed",
            extra={
                "model": self._model,
                "prompt_version": MATCH_EXPLANATION_PROMPT_VERSION,
                "latency_ms": latency_ms,
                "attempts": attempts.attempts,
                # Counts only. The statements and quotes are candidate personal
                # data and never belong in a log line.
                "conclusion_count": len(draft.conclusions),
            },
        )
        return ExplanationCall(
            draft=draft,
            model=self._model,
            prompt_version=MATCH_EXPLANATION_PROMPT_VERSION,
            latency_ms=latency_ms,
            attempts=attempts.attempts,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


def build_qwen_explanation_gateway(
    settings: Settings, *, transport: JsonTransport | None = None
) -> QwenExplanationGateway:
    """Build the real explanation gateway from settings.

    Raises ``ValueError`` when the endpoint or key is missing rather than
    degrading to the fake: a caller asking for the real adapter and receiving
    heuristic reasoning would put fabricated explanations in front of a
    recruiter, which is worse than a loud startup failure. The settings validator
    already rejects a non-mock configuration without these values, so this is
    defence in depth for direct construction.
    """
    if settings.model_base_url is None or settings.qwen_api_key is None:
        raise ValueError(
            "model_base_url and qwen_api_key are required to build the Qwen "
            "explanation gateway"
        )
    sender_transport = transport
    if sender_transport is None:
        from backend.app.infrastructure.http_transport import HttpxJsonSender

        sender_transport = JsonTransport(HttpxJsonSender(), max_attempts=3)
    return QwenExplanationGateway(
        base_url=settings.model_base_url,
        api_key=settings.qwen_api_key.get_secret_value(),
        model=settings.chat_model,
        transport=sender_transport,
        timeout_seconds=float(settings.model_timeout_seconds),
    )


__all__ = [
    "QwenExplanationGateway",
    "build_qwen_explanation_gateway",
    "parse_explanation_reply",
]
