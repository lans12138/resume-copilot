"""Shared parsing for the OpenAI-compatible chat-completions envelope.

Two gateways now speak this shape — profile extraction (``qwen_chat``) and match
explanation (``explanations.qwen_explanation``). The envelope handling lives here
rather than in either gateway so the two cannot drift: a provider that changes
its ``choices[0].message.content`` layout, or omits ``usage``, must be handled
identically by both, and the second caller is exactly where a copy would have
quietly diverged.

Everything here is total. Each function either returns a value of the declared
type or raises a permanent shape error, so callers never defend against ``None``
and never mistake an HTML error page for a completion.
"""

from __future__ import annotations

from typing import Any

from backend.app.infrastructure.http_transport import ResponseSchemaError

CHAT_COMPLETIONS_PATH = "/chat/completions"


class ChatCompletionShapeError(ResponseSchemaError):
    """The provider returned 200 but the completion envelope was unusable.

    Permanent on purpose: the same request to the same endpoint produces the same
    envelope, so retrying only burns the budget a genuine 503 would need.
    """


def extract_message_content(body: dict[str, Any]) -> str:
    """Pull the assistant text out of an OpenAI-compatible completion envelope."""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ChatCompletionShapeError("completion response carried no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ChatCompletionShapeError("completion choice was not an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ChatCompletionShapeError("completion choice carried no message")
    content = message.get("content")
    if not isinstance(content, str):
        raise ChatCompletionShapeError("completion message carried no text content")
    return content


def extract_usage(body: dict[str, Any]) -> tuple[int | None, int | None]:
    """Return ``(prompt_tokens, completion_tokens)`` when the provider reports them.

    ``usage`` is optional in the OpenAI-compatible contract and some gateways omit
    it, so a missing block is ``(None, None)`` rather than an error: token counts
    are recorded for observability, and refusing a valid completion because a
    provider did not bill it would be the tail wagging the dog. A present-but-
    malformed block is also treated as absent — the alternative is failing a call
    that already succeeded.
    """
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None, None
    return _as_optional_int(usage.get("prompt_tokens")), _as_optional_int(
        usage.get("completion_tokens")
    )


def _as_optional_int(value: Any) -> int | None:
    # ``bool`` is an ``int`` subclass, and a provider sending ``true`` here should
    # not be recorded as a token count of 1.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "ChatCompletionShapeError",
    "extract_message_content",
    "extract_usage",
]
