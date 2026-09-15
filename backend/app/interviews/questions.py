"""Deterministic interview question generator (IMP-024, §11.7).

A pure, I/O-free builder used by the ``generate_interview_questions`` graph node.
It produces *exploratory* wording (never asserts a protected attribute) and runs
every question through the sensitivity guard so the reviewer sees flags instead
of having protected topics dropped. With no evidence the questions stay generic
and the ``evidence_chunk_ids`` list is empty (§11.7, line 963).
"""

from __future__ import annotations

from uuid import UUID

from backend.app.interviews.schemas import InterviewQuestion, InterviewQuestionSet
from backend.app.interviews.sensitivity import flag_sensitivity

# Default competencies probed after a SHORTLISTED decision.
_DEFAULT_COMPETENCIES: tuple[str, ...] = ("communication", "problem_solving", "role_fit")

# Exploratory templates keyed by competency; kept assertive-free on purpose.
_TEMPLATES: dict[str, str] = {
    "communication": "请分享一个你向不同背景的同事澄清复杂问题的经历，你是如何确认对方理解的？",
    "problem_solving": "请描述一个你近期拆解过的模糊问题，你是如何界定范围并推进第一步的？",
    "role_fit": "你为什么关注这个岗位，过去哪些经历让你觉得能较快上手？",
}


def build_interview_question_set(
    *,
    competencies: tuple[str, ...] = _DEFAULT_COMPETENCIES,
    evidence_chunk_ids: list[UUID] | None = None,
) -> InterviewQuestionSet:
    """Return a versioned, sensitivity-flagged question set (deterministic)."""
    questions: list[InterviewQuestion] = []
    for i, competency in enumerate(competencies):
        text = _TEMPLATES.get(competency, f"请谈谈你在「{competency}」方面的相关经历。")
        questions.append(
            InterviewQuestion(
                question_id=f"q{i + 1}",
                question_text=text,
                competency=competency,
                rationale="基于岗位匹配结论的开放性追问；无直接断言候选人的受保护属性。",
                evidence_chunk_ids=list(evidence_chunk_ids or []),
                sensitivity_flags=flag_sensitivity(text, competency),
            )
        )
    return InterviewQuestionSet(questions=questions)
