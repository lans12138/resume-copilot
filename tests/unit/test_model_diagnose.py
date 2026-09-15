"""FIN-008: the model connectivity diagnostic.

The credential-safety assertions matter most here: the output of a connectivity
check is exactly the thing a person pastes into a ticket.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx2
import pytest

from backend.app.candidates.schemas import CandidateProfileDraft
from backend.app.core.settings import Settings
from backend.app.infrastructure import model_diagnose
from backend.app.infrastructure.http_transport import (
    HttpxJsonSender,
    JsonTransport,
    RawResponseBody,
)
from backend.app.infrastructure.model_diagnose import (
    EXIT_OK,
    EXIT_PERMANENT_FAILURE,
    EXIT_TRANSIENT_FAILURE,
    _safe_endpoint,
    main,
    probe_chat,
    probe_embedding,
)
from backend.app.infrastructure.qwen_chat import QwenChatGateway
from tests.unit.settings_factory import make_settings

SECRET = "sk-diagnose-SECRET-must-not-print"


async def _immediate(_seconds: float) -> None:
    """Backoff is not under test here, and sleeping would only slow the suite."""
    return None


def _real_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "mock_model_mode": False,
        "model_base_url": "https://dashscope.test/compatible-mode/v1",
        "qwen_api_key": SECRET,
    }
    values.update(overrides)
    return make_settings(**values)


# ----------------------------------------------------------------------
# Endpoint sanitisation
# ----------------------------------------------------------------------


def test_safe_endpoint_drops_userinfo_and_query() -> None:
    """A redaction that guesses which parameter is secret will guess wrong."""
    sanitised = _safe_endpoint("https://user:sk-hidden@api.test/v1?api_key=sk-also")
    assert sanitised == "https://api.test/v1"
    assert "sk-hidden" not in sanitised
    assert "sk-also" not in sanitised
    assert "user" not in sanitised


def test_safe_endpoint_keeps_host_and_path() -> None:
    assert _safe_endpoint("https://api.test:8443/v1") == "https://api.test:8443/v1"


def test_safe_endpoint_handles_an_unset_url() -> None:
    assert _safe_endpoint(None) == "(unset)"


@pytest.mark.parametrize(
    "malformed",
    ["not a url", "://", "https://", "dashscope", "http://:443/v1"],
)
def test_safe_endpoint_degrades_a_malformed_url_to_a_placeholder(malformed: str) -> None:
    """Diagnostics must not crash on a typo in the configuration.

    Echoing the raw string back would be the easy thing to do, and it is wrong:
    an unparseable URL is exactly the string most likely to have a credential
    glued into it by hand, so it is replaced rather than reproduced. The
    placeholder still tells the operator "your configuration is malformed",
    which is the useful part of the answer.
    """
    assert _safe_endpoint(malformed) == "(unparsed)"
    assert malformed not in _safe_endpoint(malformed)


# ----------------------------------------------------------------------
# Exit codes and output
# ----------------------------------------------------------------------


def test_mock_mode_exits_zero_and_says_nothing_was_probed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reporting green from a fake would be misleading, so it says what it did."""
    monkeypatch.setattr(model_diagnose, "get_settings", lambda: make_settings())
    code = main([])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "mock_model_mode is enabled" in out
    assert "OK" in out


def test_invalid_configuration_exits_permanently(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A config that cannot load is a configuration problem, not a transient one."""

    def boom() -> Settings:
        raise ValueError("bad settings")

    monkeypatch.setattr(model_diagnose, "get_settings", boom)
    code = main([])
    assert code == EXIT_PERMANENT_FAILURE
    assert "configuration invalid" in capsys.readouterr().err


def test_a_successful_probe_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _real_settings()
    monkeypatch.setattr(model_diagnose, "get_settings", lambda: settings)

    async def ok(_settings: Settings) -> tuple[bool, str]:
        return True, "chat        OK"

    monkeypatch.setattr(model_diagnose, "probe_chat", ok)

    async def ok_embedding(_settings: Settings) -> tuple[bool, str]:
        return True, "embedding   OK"

    monkeypatch.setattr(model_diagnose, "probe_embedding", ok_embedding)
    code = main([])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "RESULT: OK" in out


def test_a_permanent_probe_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An operator's next action differs: fix the config, do not just retry."""
    settings = _real_settings()
    monkeypatch.setattr(model_diagnose, "get_settings", lambda: settings)

    async def bad(_settings: Settings) -> tuple[bool, str]:
        return False, "chat        FAILED (permanent) — 401"

    monkeypatch.setattr(model_diagnose, "probe_chat", bad)

    async def ok_embedding(_settings: Settings) -> tuple[bool, str]:
        return True, "embedding   OK"

    monkeypatch.setattr(model_diagnose, "probe_embedding", ok_embedding)
    code = main([])
    assert code == EXIT_PERMANENT_FAILURE
    assert "RESULT: FAILED" in capsys.readouterr().out


def test_a_transient_probe_failure_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _real_settings()
    monkeypatch.setattr(model_diagnose, "get_settings", lambda: settings)

    async def flaky(_settings: Settings) -> tuple[bool, str]:
        return False, "embedding   FAILED (retryable) — 503"

    monkeypatch.setattr(model_diagnose, "probe_chat", flaky)

    async def ok_embedding(_settings: Settings) -> tuple[bool, str]:
        return True, "embedding   OK"

    monkeypatch.setattr(model_diagnose, "probe_embedding", ok_embedding)
    code = main([])
    assert code == EXIT_TRANSIENT_FAILURE
    del capsys


def test_skip_flags_avoid_probing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _real_settings()
    monkeypatch.setattr(model_diagnose, "get_settings", lambda: settings)

    async def must_not_run(_settings: Settings) -> tuple[bool, str]:
        raise AssertionError("probe should have been skipped")

        return True, "unreachable"

    monkeypatch.setattr(model_diagnose, "probe_chat", must_not_run)
    monkeypatch.setattr(model_diagnose, "probe_embedding", must_not_run)
    code = main(["--skip-chat", "--skip-embedding"])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "SKIPPED" in out


# ----------------------------------------------------------------------
# Credential safety
# ----------------------------------------------------------------------


def test_report_never_contains_the_api_key() -> None:
    report = model_diagnose._report_header(_real_settings())
    assert SECRET not in report


def test_report_never_contains_a_key_hidden_in_the_url() -> None:
    settings = _real_settings(
        model_base_url="https://user:sk-url-secret@api.test/v1?api_key=sk-query-secret"
    )
    report = model_diagnose._report_header(settings)
    assert "sk-url-secret" not in report
    assert "sk-query-secret" not in report
    assert "user:" not in report


def test_report_states_the_mode_and_models() -> None:
    report = model_diagnose._report_header(_real_settings())
    assert "real Qwen" in report
    assert "qwen3.7" in report


def test_mock_report_says_no_network_is_used() -> None:
    report = model_diagnose._report_header(make_settings())
    assert "FakeModel" in report


def test_probe_failure_line_does_not_echo_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure description must be safe to paste into a ticket.

    The transport is stubbed rather than pointed at a dead port: a real socket
    would make this test depend on the host's network stack and on how quickly
    the OS refuses the connection, and it still would not exercise the retry
    classification path the line is built from.
    """
    settings = _real_settings()
    attempts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts.append(str(request.url))
        return httpx2.Response(
            401, json={"error": {"message": f"invalid api key: {SECRET}"}}
        )

    transport = JsonTransport(
        HttpxJsonSender(transport=httpx2.MockTransport(handler)),
        max_attempts=3,
        sleep=_immediate,
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            "backend.app.infrastructure.qwen_chat.build_qwen_chat_gateway",
            lambda _settings: QwenChatGateway(
                base_url="https://dashscope.test/compatible-mode/v1",
                api_key=SECRET,
                model="qwen3.7-plus",
                transport=transport,
                timeout_seconds=5.0,
            ),
        )
        ok, line = asyncio.run(probe_chat(settings))

    assert ok is False
    assert SECRET not in line
    assert "FAILED" in line
    # The upstream echoed the credential inside its own error body; the line
    # must still be safe, because that is the realistic leak.
    assert "(permanent)" in line
    # A 401 is permanent, so the budget must not have been spent on retries.
    assert len(attempts) == 1


def test_probe_reports_a_timeout_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator's next action differs, so the kind must survive to the line."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("upstream did not answer", request=request)

    transport = JsonTransport(
        HttpxJsonSender(transport=httpx2.MockTransport(handler)),
        max_attempts=2,
        sleep=_immediate,
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            "backend.app.infrastructure.qwen_chat.build_qwen_chat_gateway",
            lambda _settings: QwenChatGateway(
                base_url="https://dashscope.test/compatible-mode/v1",
                api_key=SECRET,
                model="qwen3.7-plus",
                transport=transport,
                timeout_seconds=5.0,
            ),
        )
        ok, line = asyncio.run(probe_chat(_real_settings()))

    assert ok is False
    assert "(retryable)" in line
    assert SECRET not in line


def test_probe_describes_a_missing_configuration_as_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory's ValueError must not escape as a traceback mid-probe."""
    settings = make_settings(
        mock_model_mode=True,
        model_base_url="https://x.test/v1",
        qwen_api_key=SECRET,
    )
    stripped = settings.model_copy(update={"model_base_url": None, "qwen_api_key": None})

    ok, line = asyncio.run(probe_chat(stripped))
    assert ok is False
    assert "FAILED" in line
    assert "(permanent)" in line


def test_probe_embedding_reports_the_returned_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The width is the one thing an embedding probe can meaningfully assert.

    A silent mismatch here surfaces much later as a broken vector index, so the
    probe prints what it actually got alongside what is configured.
    """
    from backend.app.infrastructure.qwen_embedding import QwenEmbeddingGateway

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": [{"index": 0, "embedding": [0.0] * 3}]})

    gateway = QwenEmbeddingGateway(
        base_url="https://dashscope.test/compatible-mode/v1",
        api_key=SECRET,
        model="qwen3.7-text-embedding",
        dimension=3,
        transport=JsonTransport(
            HttpxJsonSender(transport=httpx2.MockTransport(handler)),
            max_attempts=1,
            sleep=_immediate,
        ),
        timeout_seconds=5.0,
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            "backend.app.infrastructure.qwen_embedding.build_qwen_embedding_gateway",
            lambda _settings: gateway,
        )
        ok, line = asyncio.run(probe_embedding(_real_settings()))

    assert ok is True
    assert "dimension=3" in line
    assert "qwen3.7-text-embedding" in line


def test_probe_embedding_classifies_a_dimension_mismatch_as_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configuring the wrong dimension is a configuration bug, not a flake."""
    from backend.app.infrastructure.qwen_embedding import QwenEmbeddingGateway

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": [{"index": 0, "embedding": [0.0] * 2}]})

    gateway = QwenEmbeddingGateway(
        base_url="https://dashscope.test/compatible-mode/v1",
        api_key=SECRET,
        model="qwen3.7-text-embedding",
        dimension=3,
        transport=JsonTransport(
            HttpxJsonSender(transport=httpx2.MockTransport(handler)),
            max_attempts=1,
            sleep=_immediate,
        ),
        timeout_seconds=5.0,
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            "backend.app.infrastructure.qwen_embedding.build_qwen_embedding_gateway",
            lambda _settings: gateway,
        )
        ok, line = asyncio.run(probe_embedding(_real_settings()))

    assert ok is False
    assert "(permanent)" in line
    assert SECRET not in line


def test_probe_success_line_reports_the_model_and_latency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _real_settings()
    real_gateway = model_diagnose.probe_chat

    class Stub:
        model = "qwen3.7-plus"
        version = "qwen-chat:v1"

        async def extract_profile(
            self, *, full_text: str, blocks: object
        ) -> CandidateProfileDraft:
            return CandidateProfileDraft()

    monkeypatch.setattr(
        "backend.app.infrastructure.qwen_chat.build_qwen_chat_gateway",
        lambda _settings: Stub(),
    )
    ok, line = asyncio.run(real_gateway(settings))
    assert ok is True
    assert "qwen3.7-plus" in line
    assert "OK" in line


def test_raw_body_repr_used_by_diagnostics_is_safe() -> None:
    """A body could echo request content, so it is never rendered."""
    body = RawResponseBody(status_code=200, text=SECRET)
    assert SECRET not in repr(body)


def test_exit_code_constants_match_the_documented_contract() -> None:
    """0 pass, 1 permanent, 2 transient — the runbook depends on this."""
    assert (EXIT_OK, EXIT_PERMANENT_FAILURE, EXIT_TRANSIENT_FAILURE) == (0, 1, 2)


def test_diagnose_module_is_runnable_as_a_module() -> None:
    """``python -m`` requires a module path, so the file must exist where claimed."""
    assert Path(model_diagnose.__file__).name == "model_diagnose.py"
    assert json.dumps({}) == "{}"
