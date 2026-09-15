"""FIN-008: recorded-response replay and synthetic dataset registration.

The point of these tests is the property that makes CI meaningful: the *real*
parsing contract is exercised with no network and no key, and the builtin
datasets are actually registered rather than merely defined.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import httpx2
import pytest

from backend.app.candidates.schemas import CandidateProfileDraft
from backend.app.core.settings import Settings
from backend.app.evaluations.datasets import (
    GOLDEN_DATASET_NAME,
    INJECTION_DATASET_NAME,
    SEMANTIC_DATASET_NAME,
    build_evaluation_metadata,
    builtin_dataset_content_digest,
    register_builtin_datasets,
)
from backend.app.evaluations.repository import (
    InMemoryDatasetVersionRepository,
    InMemoryEvaluationRunRepository,
)
from backend.app.evaluations.service import EvaluationService
from backend.app.infrastructure.http_transport import HttpxJsonSender, JsonTransport
from backend.app.infrastructure.model_gateway import FakeModelGateway
from backend.app.infrastructure.prompts import EXTRACTION_PROMPT_VERSION
from backend.app.infrastructure.qwen_chat import build_qwen_chat_gateway
from backend.app.infrastructure.recording import (
    RECORDING_SCHEMA_VERSION,
    RecordedCall,
    RecordedModelGateway,
    Recording,
    RecordingError,
    build_gateway_for_mode,
    load_recording,
    record_call,
    replay_draft,
    save_recording,
)
from tests.unit.settings_factory import make_settings

SECRET = "sk-recording-SECRET"


async def _immediate(_seconds: float) -> None:
    return None


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive a coroutine without an async plugin (none is installed here)."""
    return asyncio.run(coro)


def _service() -> EvaluationService:
    return EvaluationService(
        datasets=InMemoryDatasetVersionRepository(),
        runs=InMemoryEvaluationRunRepository(),
    )


def _reply(content: str) -> httpx2.Response:
    return httpx2.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


def _real_settings() -> Settings:
    return make_settings(
        mock_model_mode=False,
        model_base_url="https://dashscope.test/compatible-mode/v1",
        qwen_api_key=SECRET,
    )


# ----------------------------------------------------------------------
# Recording round trip
# ----------------------------------------------------------------------


def test_recording_survives_a_disk_round_trip(tmp_path: Path) -> None:
    recording = Recording(
        dataset_name="synthetic-profiles",
        dataset_version="v1",
        calls=[
            RecordedCall(
                case_id="c1",
                resume_text="张三 python",
                reply_content=json.dumps({"full_name": "张三"}),
                model="qwen3.7-plus",
                prompt_version=EXTRACTION_PROMPT_VERSION,
            )
        ],
    )
    path = tmp_path / "recording.json"
    save_recording(recording, path)
    loaded = load_recording(path)
    assert loaded.dataset_name == "synthetic-profiles"
    assert [call.case_id for call in loaded.calls] == ["c1"]
    assert loaded.prompt_versions == {EXTRACTION_PROMPT_VERSION}


def test_recording_file_carries_no_credential_or_endpoint(tmp_path: Path) -> None:
    """The fixture is committed, so it must never hold a secret."""
    recording = Recording(dataset_name="d", dataset_version="v1")
    recording.calls.append(
        RecordedCall(
            case_id="c1",
            resume_text="text",
            reply_content="{}",
            model="qwen3.7-plus",
            prompt_version=EXTRACTION_PROMPT_VERSION,
        )
    )
    path = tmp_path / "rec.json"
    save_recording(recording, path)
    raw = path.read_text(encoding="utf-8")
    assert SECRET not in raw
    assert "dashscope" not in raw


def test_loading_a_missing_recording_raises() -> None:
    with pytest.raises(RecordingError, match="not found"):
        load_recording(Path("/nonexistent/recording.json"))


def test_loading_a_malformed_recording_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RecordingError, match="not valid JSON"):
        load_recording(path)


def test_an_unsupported_schema_version_is_rejected(tmp_path: Path) -> None:
    """A layout this reader cannot interpret must not be guessed at."""
    path = tmp_path / "future.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "999",
                "dataset_name": "d",
                "dataset_version": "v1",
                "calls": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RecordingError, match="unsupported recording schema_version"):
        load_recording(path)


def test_a_recording_missing_a_call_field_raises() -> None:
    with pytest.raises(RecordingError, match="missing field"):
        RecordedCall.from_dict({"case_id": "c1"})


def test_staleness_is_detected_when_the_prompt_version_changed() -> None:
    """A stale fixture would validate an obsolete contract, so it is flagged."""
    recording = Recording(
        dataset_name="d",
        dataset_version="v1",
        calls=[
            RecordedCall(
                case_id="c1",
                resume_text="x",
                reply_content="{}",
                model="m",
                prompt_version="qwen-extract-v0",
            )
        ],
    )
    assert recording.is_stale_for(EXTRACTION_PROMPT_VERSION) is True
    assert recording.is_stale_for("qwen-extract-v0") is False


def test_recording_schema_version_is_declared() -> None:
    assert RECORDING_SCHEMA_VERSION == "1"


# ----------------------------------------------------------------------
# Recording through the real gateway
# ----------------------------------------------------------------------


def test_record_call_captures_the_raw_reply_from_the_real_gateway() -> None:
    """The raw text is stored, not a parsed draft.

    Storing the parsed draft would bake this parser's behaviour into the fixture
    and could not detect a later parser regression.
    """
    payload = json.dumps({"full_name": "张三", "skills": [{"name": "python"}]})

    def handler(request: httpx2.Request) -> httpx2.Response:
        return _reply(payload)

    gateway = build_qwen_chat_gateway(
        _real_settings(),
        transport=JsonTransport(
            HttpxJsonSender(transport=httpx2.MockTransport(handler)),
            max_attempts=1,
            sleep=_immediate,
        ),
    )

    call = _run(
        record_call(gateway, case_id="c1", resume_text="张三 python", model=gateway.model)
    )
    assert call.reply_content == payload
    assert call.prompt_version == EXTRACTION_PROMPT_VERSION
    assert call.model == gateway.model


def test_record_call_serialises_a_fake_gateway_result() -> None:
    """A non-real gateway has no raw text, so its draft is serialised instead."""

    call = _run(
        record_call(
            FakeModelGateway(),
            case_id="c1",
            # The fake's name heuristic keys on a short first line.
            resume_text="张三\npython 本科 a@b.com",
            model="fake",
        )
    )
    decoded = json.loads(call.reply_content)
    assert decoded["full_name"] == "张三"
    # And the serialised form still replays into a valid draft.
    assert parse_ok(call.reply_content)


def parse_ok(content: str) -> bool:
    from backend.app.infrastructure.qwen_chat import parse_structured_reply

    return isinstance(parse_structured_reply(content), CandidateProfileDraft)


# ----------------------------------------------------------------------
# Replay
# ----------------------------------------------------------------------


def _recording_with(case_id: str, resume: str, reply: str) -> Recording:
    return Recording(
        dataset_name="d",
        dataset_version="v1",
        calls=[
            RecordedCall(
                case_id=case_id,
                resume_text=resume,
                reply_content=reply,
                model="qwen3.7-plus",
                prompt_version=EXTRACTION_PROMPT_VERSION,
            )
        ],
    )


def test_replay_returns_the_parsed_draft() -> None:
    recording = _recording_with("c1", "张三", json.dumps({"full_name": "张三"}))
    assert replay_draft(recording, "c1").full_name == "张三"


def test_replay_of_an_unknown_case_raises() -> None:
    recording = _recording_with("c1", "张三", "{}")
    with pytest.raises(RecordingError, match="no call for case"):
        replay_draft(recording, "missing")


def test_replayed_gateway_matches_on_normalised_text() -> None:
    """A whitespace difference must not cause a spurious miss."""
    recording = _recording_with("c1", "张三   python", json.dumps({"full_name": "张三"}))
    gateway = RecordedModelGateway(recording)
    draft = _run(gateway.extract_profile(full_text="张三 python", blocks=[]))
    assert draft.full_name == "张三"


def test_replayed_gateway_raises_for_unrecorded_input() -> None:
    """A miss means the fixture is incomplete; an empty draft would pass silently."""
    recording = _recording_with("c1", "张三", "{}")
    gateway = RecordedModelGateway(recording)
    assert gateway.has_call_for("张三") is True
    assert gateway.has_call_for("someone else") is False
    with pytest.raises(RecordingError, match="no recorded response"):
        _run(gateway.extract_profile(full_text="someone else", blocks=[]))


def test_replay_exercises_the_real_parse_contract() -> None:
    """A fixture holding a malformed reply must fail on replay.

    This is what makes replay evidence rather than decoration: it goes through
    the same parsing and validation the live call uses.
    """
    recording = _recording_with("c1", "x", json.dumps({"education_level": "NOT_A_LEVEL"}))
    gateway = RecordedModelGateway(recording)
    from backend.app.infrastructure.http_transport import PermanentTransportError

    with pytest.raises(PermanentTransportError):
        _run(gateway.extract_profile(full_text="x", blocks=[]))


def test_replayed_gateway_exposes_a_version_identifying_the_fixture() -> None:
    gateway = RecordedModelGateway(_recording_with("c1", "x", "{}"))
    assert "d@v1" in gateway.version


# ----------------------------------------------------------------------
# Gateway mode selection
# ----------------------------------------------------------------------


def test_mock_mode_without_a_recording_selects_the_fake() -> None:
    assert isinstance(build_gateway_for_mode(make_settings()), FakeModelGateway)


def test_a_recording_takes_precedence_over_mock_mode(tmp_path: Path) -> None:
    """Replay is how CI exercises the real contract; it must not be replaced."""
    path = tmp_path / "rec.json"
    save_recording(_recording_with("c1", "x", "{}"), path)
    gateway = build_gateway_for_mode(make_settings(), recording_path=path)
    assert isinstance(gateway, RecordedModelGateway)


def test_non_mock_mode_without_a_recording_selects_the_real_adapter() -> None:
    gateway = build_gateway_for_mode(_real_settings())
    assert not isinstance(gateway, FakeModelGateway)
    assert not isinstance(gateway, RecordedModelGateway)


def test_a_stale_recording_still_loads_but_is_flagged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Failing hard would break CI on unrelated prompt edits; silence would hide it."""
    import logging

    stale = Recording(
        dataset_name="d",
        dataset_version="v1",
        calls=[
            RecordedCall(
                case_id="c1",
                resume_text="x",
                reply_content="{}",
                model="m",
                prompt_version="qwen-extract-v0",
            )
        ],
    )
    path = tmp_path / "stale.json"
    save_recording(stale, path)

    with caplog.at_level(logging.WARNING):
        gateway = build_gateway_for_mode(make_settings(), recording_path=path)
    assert isinstance(gateway, RecordedModelGateway)
    assert "recorded_fixture_is_stale" in caplog.text


# ----------------------------------------------------------------------
# Synthetic dataset registration
# ----------------------------------------------------------------------


def test_all_three_builtin_datasets_register() -> None:
    service = _service()
    registered = _run(register_builtin_datasets(service))
    names = {item.name for item in registered}
    assert names == {GOLDEN_DATASET_NAME, SEMANTIC_DATASET_NAME, INJECTION_DATASET_NAME}


def test_registration_is_idempotent() -> None:
    """Safe to run on every deploy: identical content returns the same rows."""
    service = _service()
    first = _run(register_builtin_datasets(service))
    second = _run(register_builtin_datasets(service))
    assert [item.content_hash for item in first] == [item.content_hash for item in second]


def test_registration_records_a_content_hash_per_dataset() -> None:
    """The hash is the integrity anchor; an empty one would defeat the purpose."""
    service = _service()
    registered = _run(register_builtin_datasets(service))
    for item in registered:
        assert len(item.content_hash) == 64
        assert item.content_hash != "0" * 64


def test_registered_datasets_are_distinguishable_by_kind() -> None:
    service = _service()
    registered = _run(register_builtin_datasets(service))
    assert {item.kind for item in registered} == {"GOLDEN", "SEMANTIC", "INJECTION"}


def test_golden_manifest_stays_a_bounded_description() -> None:
    """FIN-007 forbids case payloads in the manifest; only counts belong there."""
    service = _service()
    registered = _run(register_builtin_datasets(service))
    golden = next(item for item in registered if item.name == GOLDEN_DATASET_NAME)
    assert "cases" in golden.manifest
    assert isinstance(golden.manifest["cases"], int)
    # No case payload leaked into the manifest.
    assert "profiles" not in json.dumps(golden.manifest)


def test_dataset_digest_is_stable_across_calls() -> None:
    """A drifting digest would make the immutability guard fire spuriously."""
    assert builtin_dataset_content_digest() == builtin_dataset_content_digest()


def test_registration_is_safe_under_a_single_asyncio_run() -> None:
    """Uses no loop-bound resource, so nested invocation is fine."""

    async def main() -> list[str]:
        service = _service()
        await register_builtin_datasets(service)
        registered = await register_builtin_datasets(service)
        return [item.name for item in registered]

    assert len(_run(main())) == 3


# ----------------------------------------------------------------------
# Evaluation metadata
# ----------------------------------------------------------------------


def test_build_evaluation_metadata_is_credential_free() -> None:
    """Same rule as the evaluation route's model snapshot."""
    metadata = build_evaluation_metadata(_real_settings())
    serialised = json.dumps(metadata)
    assert SECRET not in serialised
    assert "dashscope" not in serialised


def test_build_evaluation_metadata_carries_the_version_tuple() -> None:
    metadata = build_evaluation_metadata(_real_settings())
    assert metadata["prompt_versions"]["extraction_prompt_version"] == EXTRACTION_PROMPT_VERSION
    assert metadata["model_snapshot"]["chat_model"]
    assert metadata["config_versions"]["dataset_schema_version"] == "1"


def test_metadata_records_mock_mode_so_a_run_is_attributable() -> None:
    """Without the flag, a fake run and a real run look identical in the record."""
    assert build_evaluation_metadata(make_settings())["model_snapshot"]["mock_model_mode"] is True
    assert (
        build_evaluation_metadata(_real_settings())["model_snapshot"]["mock_model_mode"] is False
    )
