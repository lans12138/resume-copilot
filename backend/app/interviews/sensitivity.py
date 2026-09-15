"""Sensitivity guard for interview questions (IMP-024, detailed design §11.7).

The guard forbids questions that probe protected attributes and requires
*exploratory* (not assertive) wording when no evidence backs a question. It is a
pure, deterministic function so nodes and the execution service can call it
without any I/O; the resulting ``sensitivity_flags`` are surfaced to the human
reviewer rather than silently dropped.
"""

from __future__ import annotations

import re

# Protected attributes a question must never target (§11.7, line 963).
PROTECTED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "age",
        "gender",
        "sex",
        "marital_status",
        "marriage",
        "pregnancy",
        "pregnant",
        "religion",
        "religious",
        "ethnicity",
        "race",
        "nationality",
        "disability",
        "health",
        "medical",
        "political",
        "political_status",
        "family",
    }
)

# Human-readable (Chinese) labels for the flags surfaced to reviewers.
_SENSITIVE_LABELS: dict[str, str] = {
    "age": "年龄",
    "gender": "性别",
    "sex": "性别",
    "marital_status": "婚姻状况",
    "marriage": "婚姻状况",
    "pregnancy": "怀孕",
    "pregnant": "怀孕",
    "religion": "宗教",
    "religious": "宗教",
    "ethnicity": "民族",
    "race": "种族",
    "nationality": "国籍",
    "disability": "残疾",
    "health": "健康",
    "medical": "健康",
    "political": "政治面貌",
    "political_status": "政治面貌",
    "family": "家庭状况",
}

# Tokeniser: split on non-word boundaries, lower-cased, keep ascii + CJK runs.
_TOKEN_RE = re.compile(r"[a-zA-Z]+|[一-鿿]+")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def flag_sensitivity(question_text: str, competency: str) -> list[str]:
    """Return the (possibly empty) list of sensitivity flags for one question.

    A flag is raised when the question text or competency references a protected
    attribute. The flags are stable, human-readable Chinese labels so the UI can
    explain *why* a question is flagged without leaking the raw token.
    """
    tokens = set(_tokens(question_text)) | {competency.lower()}
    flags: list[str] = []
    for attr in PROTECTED_ATTRIBUTES:
        # Match the attribute token directly, or as a CJK substring.
        if attr in tokens or attr in question_text.lower():
            label = _SENSITIVE_LABELS.get(attr, attr)
            if label not in flags:
                flags.append(label)
    return flags


def validate_questions(questions: list[object]) -> list[str]:
    """Aggregate sensitivity flags across a question set (de-duplicated, order-kept)."""
    seen: list[str] = []
    for q in questions:
        for flag in getattr(q, "sensitivity_flags", []):
            if flag not in seen:
                seen.append(flag)
    return seen
