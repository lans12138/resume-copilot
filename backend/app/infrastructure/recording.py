"""Recorded-response fixtures for offline evaluation replay (FIN-008).

The problem this solves: the evaluation suites are hermetic by construction, but
"hermetic" only tells the truth if the *real* adapter is exercised somewhere. If
CI only ever runs ``FakeModelGateway``, then the fake is what is being tested, and
the real prompt/schema contract can rot undetected until someone runs it by hand.

So the model's responses are recorded once against the real endpoint and checked
in as versioned JSON. Replay then feeds those exact bytes back through the same
parsing and validation path the real adapter uses. CI gets the real contract
exercised with no network and no key, and a prompt change that would break the
parse fails in CI rather than in production.

**Recordings are not payload storage for datasets.** The FIN-007 contract keeps
``dataset_versions.manifest_json`` to a bounded description and forbids case
payloads in it. These fixtures are files on disk for exactly that reason: they are
test evidence, not application state, and nothing here writes case content into
the database.

**Every recording is self-describing.** It carries the dataset identity, the
model name, and the prompt version it was captured under. A recording whose
prompt version no longer matches the live one is *stale*, and replaying it would
silently validate an obsolete contract — so staleness is detected and reported
rather than ignored. The base URL and API key are never written, matching the
model-snapshot rule the evaluation layer already enforces.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.app.candidates.schemas import CandidateProfileDraft
from backend.app.core.settings import Settings
from backend.app.documents.parsers import ParsedBlock
from backend.app.infrastructure.model_gateway import ModelGateway
from backend.app.infrastructure.prompts import EXTRACTION_PROMPT_VERSION
from backend.app.infrastructure.qwen_chat import QwenChatGateway, parse_structured_reply

logger = logging.getLogger(__name__)

# Bumped when the on-disk layout changes in a way older readers cannot handle.
RECORDING_SCHEMA_VERSION = "1"


class RecordingError(Exception):
    """A recording is malformed or does not match the request it should answer."""


@dataclass(slots=True)
class RecordedCall:
    """One captured chat exchange."""

    case_id: str
    resume_text: str
    reply_content: str
    model: str
    prompt_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "resume_text": self.resume_text,
            "reply_content": self.reply_content,
            "model": self.model,
            "prompt_version": self.prompt_version,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RecordedCall:
        try:
            return cls(
                case_id=str(raw["case_id"]),
                resume_text=str(raw["resume_text"]),
                reply_content=str(raw["reply_content"]),
                model=str(raw["model"]),
                prompt_version=str(raw["prompt_version"]),
            )
        except KeyError as error:
            raise RecordingError(f"recording is missing field {error}") from error


@dataclass(slots=True)
class Recording:
    """A versioned bundle of captured exchanges for one dataset."""

    dataset_name: str
    dataset_version: str
    schema_version: str = RECORDING_SCHEMA_VERSION
    calls: list[RecordedCall] = field(default_factory=list)

    @property
    def prompt_versions(self) -> set[str]:
        return {call.prompt_version for call in self.calls}

    def is_stale_for(self, live_prompt_version: str) -> bool:
        """True when any call was captured under a different prompt version.

        A stale recording still replays — it will simply exercise the old
        contract — so this is surfaced as a warning rather than raised. Failing
        hard would break CI on unrelated prompt edits; staying silent would let a
        stale fixture masquerade as current evidence.
        """
        return any(
            call.prompt_version != live_prompt_version for call in self.calls
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dataset_name": self.dataset_name,
            "dataset_version": self.dataset_version,
            "prompt_versions": sorted(self.prompt_versions),
            "calls": [call.to_dict() for call in self.calls],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Recording:
        schema_version = str(raw.get("schema_version", ""))
        if schema_version != RECORDING_SCHEMA_VERSION:
            raise RecordingError(
                f"unsupported recording schema_version {schema_version!r}; "
                f"expected {RECORDING_SCHEMA_VERSION!r}"
            )
        try:
            calls = [
                RecordedCall.from_dict(item)
                for item in _require_list(raw.get("calls"), "calls")
            ]
            return cls(
                dataset_name=str(raw["dataset_name"]),
                dataset_version=str(raw["dataset_version"]),
                schema_version=schema_version,
                calls=calls,
            )
        except KeyError as error:
            raise RecordingError(f"recording is missing field {error}") from error


def _require_list(value: object, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise RecordingError(f"recording field {label!r} must be a list")
    for item in value:
        if not isinstance(item, Mapping):
            raise RecordingError(f"recording field {label!r} must hold objects")
    return list(value)


def save_recording(recording: Recording, path: Path) -> None:
    """Write a recording as UTF-8 JSON, with a stable key order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(recording.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_recording(path: Path) -> Recording:
    """Read a recording; raises :class:`RecordingError` on a malformed payload."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RecordingError(f"recording not found: {path}") from error
    except ValueError as error:
        raise RecordingError(f"recording is not valid JSON: {path}") from error
    if not isinstance(raw, Mapping):
        raise RecordingError("recording must be a JSON object")
    return Recording.from_dict(raw)


def replay_draft(recording: Recording, case_id: str) -> CandidateProfileDraft:
    """Parse one recorded reply through the real adapter's parsing path.

    Going through ``parse_structured_reply`` rather than a shortcut is the whole
    point: replay validates the same JSON extraction, fence handling and Pydantic
    schema the live call uses, so a contract break shows up here.
    """
    for call in recording.calls:
        if call.case_id == case_id:
            return parse_structured_reply(call.reply_content)
    raise RecordingError(f"recording has no call for case {case_id!r}")


class RecordedModelGateway:
    """``ModelGateway`` that answers from a recording instead of the network.

    Used in CI to exercise the real parsing path without an endpoint. Matching is
    on a normalised form of the resume text so that a trailing-newline difference
    between recorder and replayer does not cause a spurious miss.
    """

    def __init__(self, recording: Recording) -> None:
        self.version = f"recorded:{recording.dataset_name}@{recording.dataset_version}"
        self._by_text = {
            _normalise(call.resume_text): call for call in recording.calls
        }
        self._recording = recording

    @property
    def recording(self) -> Recording:
        return self._recording

    def has_call_for(self, resume_text: str) -> bool:
        return _normalise(resume_text) in self._by_text

    async def extract_profile(
        self, *, full_text: str, blocks: Sequence[ParsedBlock]
    ) -> CandidateProfileDraft:
        call = self._by_text.get(_normalise(full_text))
        if call is None:
            # A miss means the fixture no longer covers the input: the recording
            # is incomplete, not the model. Failing loudly beats returning an
            # empty draft that would silently pass an evaluation.
            raise RecordingError(
                "no recorded response for this input; re-record the fixture "
                f"after a prompt or dataset change (dataset "
                f"{self._recording.dataset_name}@{self._recording.dataset_version})"
            )
        return parse_structured_reply(call.reply_content)


def _normalise(text: str) -> str:
    return " ".join(text.split())


async def record_call(
    gateway: ModelGateway,
    *,
    case_id: str,
    resume_text: str,
    model: str,
    prompt_version: str = EXTRACTION_PROMPT_VERSION,
) -> RecordedCall:
    """Call a gateway once and capture the raw reply for later replay.

    The reply is stored as the *raw assistant text*, before parsing, so a replay
    re-runs the parse. Storing the parsed draft instead would bake the current
    parser's behaviour into the fixture and hide a later parser regression.
    """
    if isinstance(gateway, QwenChatGateway):
        # Capture the raw text by repeating the request through the transport
        # layer, so what is stored is exactly what the provider returned.
        raw_call = await _capture_raw(gateway, resume_text)
        return RecordedCall(
            case_id=case_id,
            resume_text=resume_text,
            reply_content=raw_call,
            model=model,
            prompt_version=prompt_version,
        )

    # Any other gateway (the fake, or a custom double) has no raw text, so its
    # structured result is serialised back to JSON. Replay then validates that
    # same document, which keeps the fixture usable without a second parser.
    draft = await gateway.extract_profile(full_text=resume_text, blocks=[])
    return RecordedCall(
        case_id=case_id,
        resume_text=resume_text,
        reply_content=json.dumps(draft.model_dump(mode="json"), ensure_ascii=False),
        model=model,
        prompt_version=prompt_version,
    )


async def _capture_raw(gateway: QwenChatGateway, resume_text: str) -> str:
    """Return the assistant text verbatim for one resume."""
    return await gateway.complete_extraction_raw(full_text=resume_text)


def build_gateway_for_mode(
    settings: Settings, *, recording_path: Path | None = None
) -> ModelGateway:
    """Select fake, recorded, or real extraction gateway.

    Three explicit modes, in this order:

    * ``recording_path`` given -> replay. Checked-in evidence wins, because a
      caller that named a fixture wants determinism, not the network.
    * ``mock_model_mode`` -> the deterministic fake (the ordinary CI default).
    * otherwise -> the real adapter.

    ``recording_path`` deliberately takes precedence over mock mode: replay is how
    CI exercises the real parsing contract, and it must not be silently replaced
    by the heuristic fake just because mock mode is on.
    """
    if recording_path is not None:
        recording = load_recording(recording_path)
        if recording.is_stale_for(EXTRACTION_PROMPT_VERSION):
            logger.warning(
                "recorded_fixture_is_stale",
                extra={
                    "dataset": f"{recording.dataset_name}@{recording.dataset_version}",
                    "recorded_prompt_versions": sorted(recording.prompt_versions),
                    "live_prompt_version": EXTRACTION_PROMPT_VERSION,
                },
            )
        return RecordedModelGateway(recording)
    if settings.mock_model_mode:
        from backend.app.infrastructure.model_gateway import FakeModelGateway

        return FakeModelGateway()
    from backend.app.infrastructure.qwen_chat import build_qwen_chat_gateway

    return build_qwen_chat_gateway(settings)


__all__ = [
    "RECORDING_SCHEMA_VERSION",
    "RecordedCall",
    "RecordedModelGateway",
    "Recording",
    "RecordingError",
    "build_gateway_for_mode",
    "load_recording",
    "record_call",
    "replay_draft",
    "save_recording",
]
