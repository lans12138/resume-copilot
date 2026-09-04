"""Interview questions and mock schedule (IMP-024).

This package owns the second-approval side effect: the interview question set
(detailed design §11.7), its sensitivity guard, and the ``MockScheduleBackend``
that generates a *stable* external schedule id inside a controlled transaction
boundary (§11.7, line 967-975). The backend is a Protocol so a real calendar
integration can later replace it without touching the execution service.

The execution service that wires these to an ``Approval`` lives in
``backend.app.job_applications.side_effects`` (it must apply the side effect
exactly once, gated by the approval idempotency key).
"""

from __future__ import annotations

from backend.app.interviews.models import Interview
from backend.app.interviews.schedule import MockScheduleBackend, ScheduleBackend
from backend.app.interviews.schemas import (
    CreateInterviewScheduleCommand,
    InterviewQuestion,
    InterviewQuestionSet,
    ScheduleProposal,
    ScheduleResult,
)

__all__ = [
    "Interview",
    "MockScheduleBackend",
    "ScheduleBackend",
    "ScheduleProposal",
    "ScheduleResult",
    "InterviewQuestion",
    "InterviewQuestionSet",
    "CreateInterviewScheduleCommand",
]
