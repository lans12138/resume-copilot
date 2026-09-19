"""Match-explanation gateway: the model boundary for PORT-003.

The gateway answers one question — "given this job and this candidate's evidence,
what job-relevant statements can be made, and which excerpts back them?" It has
no authority beyond that: it returns a draft, and ``explanations.service`` decides
what survives verification and what reaches the report.

The fake and the real adapter are chosen by the same ``mock_model_mode`` flag the
extraction gateway uses, so a run cannot be half-real. ``FakeMatchExplanationGateway``
is deterministic and *job-dependent* on purpose: it matches the job's required
skills against the candidate's chunk text, so "same candidate, different job,
different explanation" is testable with no network and no key. A fake that
returned a canned string would make that acceptance criterion untestable offline,
which is the same trap ``FakeModelGateway`` avoids by actually extracting.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from backend.app.core.settings import Settings
from backend.app.explanations.contract import (
    DEFAULT_MODEL_IMPACT,
    ExplanationCitation,
    ExplanationConclusion,
    MatchExplanationDraft,
)
from backend.app.infrastructure.prompts import MATCH_EXPLANATION_PROMPT_VERSION

# Bounded prompt budget, matching the extraction gateway's rule: the snapshot
# records this, so changing it changes what an evaluation measured.
MAX_EXPLANATION_PROMPT_CHARS = 24_000

# How many of the job's required skills the fake comments on. Bounded by the
# schema's conclusion cap, with room left for the summary line.
_FAKE_MAX_CONCLUSIONS = 5


@dataclass(frozen=True)
class EvidenceItem:
    """One candidate chunk offered to the model, with the id it must cite."""

    chunk_id: UUID
    text: str


@dataclass(frozen=True)
class ExplanationRequest:
    """Everything one explanation call needs.

    The structured fields exist alongside the rendered text because a gateway
    must be able to reason about the *job* without re-parsing the prompt it was
    handed. ``job_text``/``verdict_text`` are what the real adapter sends;
    ``required_skills`` is what the fake matches on. Keeping both on one request
    means a fake can never disagree with the real adapter about what the input
    was.
    """

    job_text: str
    verdict_text: str
    required_skills: tuple[str, ...]
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class ExplanationCall:
    """One gateway call's result *and* its cost, as recorded for observability.

    The metadata travels with the draft rather than being read back off the
    gateway, so a concurrent run cannot attribute one call's latency or token
    usage to another's output.
    """

    draft: MatchExplanationDraft
    model: str
    prompt_version: str
    latency_ms: int
    attempts: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    clamped_impact_count: int = 0
    notes: tuple[str, ...] = ()


class MatchExplanationGateway(Protocol):
    """Async contract for job-relevant, citation-bearing explanations."""

    version: str
    #: The bare prompt version this gateway sends. Recorded per call, so a run can
    #: never claim a prompt it did not use (``prompts.py`` states the same rule
    #: for the extraction adapter).
    prompt_version: str

    async def explain(self, request: ExplanationRequest) -> ExplanationCall:
        """Return a schema-valid draft; never trust the reply's own claims."""
        ...


def _skill_pattern(skill: str) -> re.Pattern[str]:
    """Whole-token matcher, alphanumeric boundaries only.

    Same boundary rule as the report's evidence binding, and for the same reason:
    ``java`` must not match inside ``javascript``, while ``c++`` and ``node.js``
    still have to match. The two implementations are deliberately consistent —
    a fake that found skills the real binder would not (or vice versa) would make
    the fake's conclusions unverifiable for a reason that has nothing to do with
    the model.

    ``re.IGNORECASE`` rather than a casefolded haystack on purpose: casefolding
    can change a string's length (``ß`` → ``ss``), which would shift the match
    offsets and make the sliced quote no longer occur in the chunk verbatim —
    exactly the failure the verifier downstream would reject.
    """
    return re.compile(
        rf"(?<![a-z0-9]){re.escape(skill.strip())}(?![a-z0-9])", re.IGNORECASE
    )


def find_skill_quote(text: str, skill: str) -> str | None:
    """Return the verbatim substring of ``text`` that states ``skill``.

    Verbatim matters: the quote is re-checked against the chunk before anything
    is persisted, so a gateway that returned a normalised or translated version
    would produce citations the server rejects.
    """
    if not skill.strip():
        return None
    match = _skill_pattern(skill).search(text)
    if match is None:
        return None
    return text[match.start() : match.end()]


class FakeMatchExplanationGateway:
    """Deterministic, key-free explanation gateway for local runs and CI.

    It produces exactly the kind of output the real adapter is asked for —
    conclusions that cite a chunk and quote it verbatim — and it produces
    *nothing* when the job's requirements do not appear in the evidence. That
    second property is what keeps it honest: the fake is not allowed to invent a
    reason that the evidence does not contain.
    """

    def __init__(self, *, model: str = "fake-explain") -> None:
        self._model = model
        self.version = f"{model}:{MATCH_EXPLANATION_PROMPT_VERSION}"
        self.prompt_version = MATCH_EXPLANATION_PROMPT_VERSION

    @property
    def model(self) -> str:
        return self._model

    async def explain(self, request: ExplanationRequest) -> ExplanationCall:
        started = time.perf_counter()
        conclusions: list[ExplanationConclusion] = []
        located: list[str] = []

        for skill in request.required_skills:
            if len(conclusions) >= _FAKE_MAX_CONCLUSIONS:
                break
            for item in request.evidence:
                quote = find_skill_quote(item.text, skill)
                if quote is None:
                    continue
                located.append(skill)
                conclusions.append(
                    ExplanationConclusion(
                        statement=f"岗位要求的 {skill} 可在候选人原文中定位到。",
                        impact=DEFAULT_MODEL_IMPACT,
                        citations=[
                            ExplanationCitation(chunk_id=item.chunk_id, quote=quote)
                        ],
                    )
                )
                break

        total = len(request.required_skills)
        summary = (
            f"岗位共要求 {total} 项技能，其中 {len(located)} 项可在候选人证据中定位到"
            f"（{('、'.join(located)) if located else '无'}）。"
            f"硬性条件结论为固定上下文，未在此重述。"
        )
        draft = MatchExplanationDraft(summary=summary, conclusions=conclusions)
        return ExplanationCall(
            draft=draft,
            model=self._model,
            prompt_version=MATCH_EXPLANATION_PROMPT_VERSION,
            latency_ms=_elapsed_ms(started),
            attempts=1,
            notes=("fake_gateway",),
        )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def build_explanation_gateway(settings: Settings) -> MatchExplanationGateway:
    """Return the explanation gateway selected by configuration.

    Same single-flag contract as ``build_model_gateway``: mock resolves to the
    deterministic fake, non-mock to the real adapter, and neither branch falls
    back to the other. A misconfigured non-mock deployment must fail loudly —
    silently explaining with the fake would put fabricated reasoning in front of
    a recruiter with nothing marking it as such.
    """
    if settings.mock_model_mode:
        return FakeMatchExplanationGateway()
    from backend.app.explanations.qwen_explanation import (
        build_qwen_explanation_gateway,
    )

    return build_qwen_explanation_gateway(settings)


__all__ = [
    "MAX_EXPLANATION_PROMPT_CHARS",
    "EvidenceItem",
    "ExplanationCall",
    "ExplanationRequest",
    "FakeMatchExplanationGateway",
    "MatchExplanationGateway",
    "build_explanation_gateway",
    "find_skill_quote",
]
