"""Hermetic tests for the real match-explanation adapter (PORT-003).

Runs against ``httpx2.MockTransport``, so the request the adapter builds and the
way it classifies what comes back are both exercised through the real client path
with no network and no key. The gate checks four things the explanation loop
depends on:

* the adapter is selected by the same single ``mock_model_mode`` flag as every
  other gateway, and a non-mock configuration without an endpoint fails loudly
  rather than degrading to the fake;
* the request is the OpenAI-compatible shape the provider expects, with the key
  only in the ``authorization`` header and the candidate's text never in a log;
* usage and attempt counts are recorded when the provider reports them and left
  null when it does not — never estimated;
* a 401 is permanent and a 429 is retryable, so the reason code the service
  records is decided by the transport, not by message text.

``chat_completion`` is covered here too: it is the shared envelope parser both
this adapter and ``qwen_chat`` use, and the reason it exists is that the second
caller is exactly where a copy would have quietly diverged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import uuid4

import httpx2
import pytest

from backend.app.core.settings import Settings
from backend.app.explanations.contract import MatchExplanationDraft
from backend.app.explanations.gateway import (
    MAX_EXPLANATION_PROMPT_CHARS,
    EvidenceItem,
    ExplanationRequest,
    FakeMatchExplanationGateway,
    build_explanation_gateway,
)
from backend.app.explanations.qwen_explanation import (
    QwenExplanationGateway,
    build_qwen_explanation_gateway,
    parse_explanation_reply,
)
from backend.app.infrastructure.chat_completion import (
    ChatCompletionShapeError,
    extract_message_content,
    extract_usage,
)
from backend.app.infrastructure.http_transport import (
    HttpxJsonSender,
    JsonTransport,
    PermanentTransportError,
    RetryableTransportError,
)
from backend.app.infrastructure.prompts import MATCH_EXPLANATION_PROMPT_VERSION
from tests.unit.settings_factory import make_settings

SECRET = "sk-test-SECRET-do-not-leak"
CANDIDATE_TEXT = "工作经历：5 年 Python 后端开发"

Handler = Callable[[httpx2.Request], httpx2.Response]


async def _immediate(_seconds: float) -> None:
    return None


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive a coroutine without an async plugin (none is installed here)."""
    return asyncio.run(coro)


def _transport(handler: Handler, *, max_attempts: int = 2) -> JsonTransport:
    return JsonTransport(
        HttpxJsonSender(transport=httpx2.MockTransport(handler)),
        max_attempts=max_attempts,
        sleep=_immediate,
    )


def _completion(
    content: str, *, usage: dict[str, Any] | None = None
) -> httpx2.Response:
    body: dict[str, Any] = {
        "choices": [{"message": {"role": "assistant", "content": content}}]
    }
    if usage is not None:
        body["usage"] = usage
    return httpx2.Response(200, json=body)


def _reply(*, statement: str = "候选人有相关经验。") -> str:
    return json.dumps(
        {
            "summary": "候选人整体匹配情况",
            "conclusions": [
                {"statement": statement, "impact": "MEDIUM", "citations": []}
            ],
        }
    )


def _gateway(handler: Handler, **overrides: Any) -> QwenExplanationGateway:
    settings = make_settings(
        mock_model_mode=False,
        model_base_url="https://dashscope.example/v1",
        qwen_api_key=SECRET,
        **overrides,
    )
    return build_qwen_explanation_gateway(
        settings, transport=_transport(handler)
    )


def _request(*, required_skills: tuple[str, ...] = ("python",)) -> ExplanationRequest:
    return ExplanationRequest(
        job_text="岗位：Python 后端工程师",
        verdict_text="满足年限要求",
        required_skills=required_skills,
        evidence=(EvidenceItem(chunk_id=uuid4(), text=CANDIDATE_TEXT),),
    )


# ----------------------------------------------------------------------
# Factory selection: one flag, no silent fallback
# ----------------------------------------------------------------------


def test_mock_mode_selects_the_fake_explanation_gateway() -> None:
    gateway = build_explanation_gateway(make_settings())
    assert isinstance(gateway, FakeMatchExplanationGateway)


def test_non_mock_mode_selects_the_real_explanation_gateway() -> None:
    gateway = build_explanation_gateway(
        make_settings(
            mock_model_mode=False,
            model_base_url="https://dashscope.example/v1",
            qwen_api_key=SECRET,
        )
    )
    assert isinstance(gateway, QwenExplanationGateway)


def test_real_factory_refuses_a_missing_endpoint() -> None:
    """Degrading to the fake would put heuristic reasoning in front of a recruiter."""
    with pytest.raises(ValueError):
        build_qwen_explanation_gateway(
            Settings.model_construct(
                mock_model_mode=False,
                model_base_url=None,
                qwen_api_key=None,
                chat_model="qwen3.7-plus",
                model_timeout_seconds=60,
            )
        )


def test_gateway_version_records_the_model_and_the_prompt() -> None:
    """A run may not claim a prompt it did not use."""
    gateway = _gateway(lambda _request: _completion(_reply()))
    assert gateway.prompt_version == MATCH_EXPLANATION_PROMPT_VERSION
    assert "qwen-explain" in gateway.version
    assert MATCH_EXPLANATION_PROMPT_VERSION in gateway.version


# ----------------------------------------------------------------------
# The request the adapter builds
# ----------------------------------------------------------------------


def test_payload_is_the_openai_compatible_shape() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return _completion(_reply())

    _run(_gateway(handler).explain(_request()))

    assert len(seen) == 1
    assert seen[0].url.path.endswith("/chat/completions")
    payload = json.loads(seen[0].content)
    assert payload["temperature"] == 0
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["role"] == "user"


def test_the_key_is_only_in_the_authorization_header() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return _completion(_reply())

    _run(_gateway(handler).explain(_request()))

    assert seen[0].headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in str(seen[0].url)
    assert SECRET not in seen[0].content.decode()


def test_the_prompt_carries_the_verdict_as_fixed_context_and_the_evidence_with_ids() -> None:
    seen: list[httpx2.Request] = []
    request = _request()

    def handler(incoming: httpx2.Request) -> httpx2.Response:
        seen.append(incoming)
        return _completion(_reply())

    _run(_gateway(handler).explain(request))

    user_prompt = json.loads(seen[0].content)["messages"][1]["content"]
    assert "满足年限要求" in user_prompt
    assert "<verdict>" in user_prompt
    # The chunk id is rendered next to the text, so the model has one less thing
    # to get wrong when it cites.
    assert str(request.evidence[0].chunk_id) in user_prompt
    assert CANDIDATE_TEXT in user_prompt


def test_an_oversized_evidence_set_is_truncated_and_flagged() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return _completion(_reply())

    huge = ExplanationRequest(
        job_text="岗位",
        verdict_text="结论",
        required_skills=("python",),
        evidence=tuple(
            EvidenceItem(chunk_id=uuid4(), text="x" * MAX_EXPLANATION_PROMPT_CHARS)
            for _ in range(3)
        ),
    )
    _run(_gateway(handler).explain(huge))

    user_prompt = json.loads(seen[0].content)["messages"][1]["content"]
    assert "some evidence was omitted" in user_prompt


# ----------------------------------------------------------------------
# What the adapter does with the reply
# ----------------------------------------------------------------------


def test_success_records_usage_and_the_attempt_count() -> None:
    gateway = _gateway(
        lambda _request: _completion(
            _reply(),
            usage={"prompt_tokens": 321, "completion_tokens": 45},
        )
    )
    call = _run(gateway.explain(_request()))
    assert call.prompt_tokens == 321
    assert call.completion_tokens == 45
    assert call.attempts == 1
    assert isinstance(call.draft, MatchExplanationDraft)
    assert call.latency_ms >= 0


def test_usage_is_left_null_when_the_provider_omits_it() -> None:
    """Token counts are observability, never estimated: a fabricated count would be
    indistinguishable from a real one in the metrics that consume it."""
    call = _run(_gateway(lambda _request: _completion(_reply())).explain(_request()))
    assert call.prompt_tokens is None
    assert call.completion_tokens is None


def test_a_retried_call_reports_how_many_attempts_it_took() -> None:
    calls = {"n": 0}

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx2.Response(429, json={"error": "slow down"})
        return _completion(_reply())

    call = _run(_gateway(handler).explain(_request()))
    assert calls["n"] == 2
    assert call.attempts == 2


def test_a_401_is_permanent_and_a_429_is_retryable() -> None:
    with pytest.raises(PermanentTransportError):
        _run(
            _gateway(lambda _request: httpx2.Response(401, json={})).explain(
                _request()
            )
        )
    with pytest.raises(RetryableTransportError):
        _run(
            _gateway(
                lambda _request: httpx2.Response(429, json={}), max_attempts=2
            ).explain(_request())
        )


def test_a_reply_that_is_not_an_object_is_a_schema_failure() -> None:
    with pytest.raises(ChatCompletionShapeError):
        _run(
            _gateway(lambda _request: _completion("[1, 2, 3]")).explain(_request())
        )


def test_the_adapter_never_logs_the_key_or_the_candidate_text() -> None:
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("backend.app.explanations.qwen_explanation")
    capture = Capture()
    logger.addHandler(capture)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        _run(
            _gateway(
                lambda _request: _completion(_reply(statement=CANDIDATE_TEXT))
            ).explain(_request())
        )
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous)

    scanned = " ".join(
        [record.getMessage() for record in records]
        + [
            f"{key}={value}"
            for record in records
            for key, value in vars(record).items()
        ]
    )
    assert SECRET not in scanned
    assert CANDIDATE_TEXT not in scanned
    # The count is still logged, which is the useful part.
    assert any(getattr(record, "conclusion_count", None) == 1 for record in records)


# ----------------------------------------------------------------------
# The shared envelope parser
# ----------------------------------------------------------------------


def test_extract_message_content_rejects_every_unusable_envelope() -> None:
    unusable: tuple[dict[str, Any], ...] = (
        {},
        {"choices": []},
        {"choices": ["nope"]},
        {"choices": [{}]},
        {"choices": [{"message": {"content": None}}]},
    )
    for body in unusable:
        with pytest.raises(ChatCompletionShapeError):
            extract_message_content(body)

def test_extract_usage_returns_none_rather_than_guessing() -> None:
    assert extract_usage({}) == (None, None)
    assert extract_usage({"usage": "not an object"}) == (None, None)
    assert extract_usage({"usage": {"prompt_tokens": "12"}}) == (None, None)
    # ``bool`` is an ``int`` subclass; a provider sending ``true`` must not be
    # recorded as a token count of 1.
    assert extract_usage({"usage": {"prompt_tokens": True}}) == (None, None)
    assert extract_usage({"usage": {"prompt_tokens": -3}}) == (None, None)
    assert extract_usage(
        {"usage": {"prompt_tokens": 10, "completion_tokens": 4}}
    ) == (10, 4)


def test_parse_explanation_reply_accepts_an_empty_conclusion_list() -> None:
    """ "I cannot say anything job-relevant" is a valid, expected answer."""
    draft = parse_explanation_reply('{"summary": "证据不足", "conclusions": []}')
    assert draft.conclusions == []
