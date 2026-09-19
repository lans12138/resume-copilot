"""Match-explanation contract produced by the model (PORT-003).

This module is the boundary between untrusted model output and the trusted
report. Everything the model returns is a *proposal*; nothing here grants it
authority over the decision. Three rules from the requirement set shape the
schema, and each is enforced structurally rather than by asking the model
nicely:

**BR-002 — the model explains, it does not decide.** A conclusion carries no
verdict, no recommendation and no score. It carries a statement plus the
citations it rests on. The deterministic hard-rule verdicts are *input* to the
prompt, never something the model may recompute or overwrite.

**BR-001 / §4.5 — the model cannot escalate impact.** ``impact`` is a closed
``LOW | MEDIUM`` set. ``HIGH`` is reserved for a failing deterministic hard rule,
the only thing that may move a candidate out of the shortlist. This is why the
schema *rejects* a ``HIGH`` reply instead of clamping it: the §4.5 guard only
rewrites high-impact claims that are not SUPPORTED, so a model able to set
``HIGH`` and cite a legal excerpt could assert a decisive verdict through the
back door. A reply that tries is a contract violation — permanent, recorded, and
visible — not a near-miss to be quietly patched up.

**BR-011 — no sensitive attributes.** The schema is closed (``extra="forbid"``)
and every text field is length-bounded, so a model cannot smuggle a new field —
age, gender, ethnicity — into the report by inventing one. The prompt forbids it
too, but a prompt is a request and a schema is a guarantee.

Citations are *claims about* the evidence, not evidence. Each is re-verified
against the candidate's own chunks before anything is persisted
(``explanations.service``), and a conclusion that loses every citation is
dropped rather than stored unbacked.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Bounds chosen so one reply stays reviewable by a human. A model asked for "all
# the reasons" will happily return thirty; a report with thirty model claims is
# not a report anyone reads.
MAX_CONCLUSIONS = 8
MAX_CITATIONS_PER_CONCLUSION = 3
MAX_STATEMENT_CHARS = 500
MAX_SUMMARY_CHARS = 1_200
MAX_QUOTE_CHARS = 500

# The impact levels a model-authored conclusion may carry (§4.5, BR-001).
# ``HIGH`` is deliberately absent; see the module docstring.
ModelImpact = Literal["LOW", "MEDIUM"]

DEFAULT_MODEL_IMPACT: ModelImpact = "MEDIUM"


class ExplanationCitation(BaseModel):
    """One excerpt the model says supports its statement.

    ``chunk_id`` must name a chunk belonging to the candidate under discussion.
    The model is shown that candidate's chunk ids and told to use only those; a
    reply that invents one fails verification rather than being trusted.
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)

    @field_validator("quote")
    @classmethod
    def quote_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("quote must not be blank")
        return value


class ExplanationConclusion(BaseModel):
    """One job-relevant statement the model makes about a candidate."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=MAX_STATEMENT_CHARS)
    impact: ModelImpact = DEFAULT_MODEL_IMPACT
    citations: list[ExplanationCitation] = Field(
        default_factory=list, max_length=MAX_CITATIONS_PER_CONCLUSION
    )

    @field_validator("statement")
    @classmethod
    def statement_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("statement must not be blank")
        return value.strip()


class MatchExplanationDraft(BaseModel):
    """The whole model reply: one summary plus bounded, cited conclusions."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    conclusions: list[ExplanationConclusion] = Field(
        default_factory=list, max_length=MAX_CONCLUSIONS
    )

    @field_validator("summary")
    @classmethod
    def summary_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must not be blank")
        return value.strip()


__all__ = [
    "DEFAULT_MODEL_IMPACT",
    "MAX_CITATIONS_PER_CONCLUSION",
    "MAX_CONCLUSIONS",
    "MAX_QUOTE_CHARS",
    "MAX_STATEMENT_CHARS",
    "MAX_SUMMARY_CHARS",
    "ExplanationCitation",
    "ExplanationConclusion",
    "MatchExplanationDraft",
    "ModelImpact",
]
