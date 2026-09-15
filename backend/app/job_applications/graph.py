"""ApplicationRun graph definition (IMP-021/024).

Fixed graph (detailed design §10/§11), the two-approval flow:

    load_application → analyze_candidate → human_review [interrupt #1: WAITING_APPROVAL]
        REJECTED → complete_without_change → END   (handled by the decide service)
        APPROVED/EDITED → update_application_status →
            target != SHORTLISTED → finalize_run → END
            target == SHORTLISTED → generate_interview_questions
                                 → wait_schedule_approval [interrupt #2, guarded]
                REJECTED → complete_without_schedule → END
                APPROVED/EDITED → create_interview_schedule → finalize_run → END

Node #1 (``human_review``) is the primary interrupt: it always pauses on first
execution. Node #2 (``wait_schedule_approval``) is a *conditional* interrupt —
the engine pauses there only when the run reached ``SHORTLISTED`` and so needs a
second approval (§11.7). Both pauses keep the active slot occupied until a human
decision; the side-effect nodes never write tables themselves (detailed design
§11.6/§11.7, line 151) — they only set typed state, and the executing service
applies the mutation exactly once, gated by the approval idempotency key.

Every node is a pure ``(state) -> state`` transform so the engine can replay and
interrupt deterministically (§17.2). The proposals/questions are typed data only.
"""

from __future__ import annotations

from typing import Any

from backend.app.agent.engine import GraphNode, RunGraph
from backend.app.agent.models import RunStatus
from backend.app.interviews.questions import build_interview_question_set
from backend.app.interviews.schemas import QUESTION_SCHEMA_VERSION, InterviewQuestionSet


def _target_status(state: dict[str, Any]) -> str:
    return str(state.get("proposed_status", "SHORTLISTED")).upper()


def build_application_graph() -> RunGraph:
    """Return the two-approval ApplicationRun graph (§10)."""

    def load_application(state: dict[str, Any]) -> dict[str, Any]:
        # Pure read: pin the application context the later nodes need.
        return {**state, "application_loaded": True}

    def analyze_candidate(state: dict[str, Any]) -> dict[str, Any]:
        # No side effects. Build a typed proposal the human will review.
        proposal = {
            "action_type": "UPDATE_APPLICATION_STATUS",
            "original_params": {
                "target_status": state.get("proposed_status", "SHORTLISTED"),
            },
        }
        return {**state, "proposal": proposal, "proposal_ready": True}

    def human_review(state: dict[str, Any]) -> dict[str, Any]:
        # Freeze the proposal for the first (status) pending approval (IMP-022).
        return {**state, "pending_approval": state.get("proposal")}

    def update_application_status(state: dict[str, Any]) -> dict[str, Any]:
        # Pure: pin the decided target and whether a second approval is needed.
        # The actual JobApplication mutation is applied by the execution service.
        target = _target_status(state)
        needs_interview = target == "SHORTLISTED"
        return {
            **state,
            "target_status": target,
            "needs_interview": needs_interview,
            "status_updated": True,
        }

    def generate_interview_questions(state: dict[str, Any]) -> dict[str, Any]:
        # Pure: build a deterministic, sensitivity-flagged question set only when
        # the run reached SHORTLISTED. Stored in state (checkpoint), not written to
        # a table by the node.
        if not state.get("needs_interview"):
            return {**state, "question_set": None}
        question_set: InterviewQuestionSet = build_interview_question_set()
        return {
            **state,
            "question_set": question_set.model_dump(),
            "question_schema_version": QUESTION_SCHEMA_VERSION,
        }

    def wait_schedule_approval(state: dict[str, Any]) -> dict[str, Any]:
        # Pure: mark that a second (schedule) approval is required. The approval
        # object itself is created by the decide service when the run pauses here.
        if state.get("needs_interview"):
            return {**state, "pending_schedule_approval": True}
        return state

    def create_interview_schedule(state: dict[str, Any]) -> dict[str, Any]:
        # Pure: mark the schedule side effect as required. The interview row and
        # JobApplication=INTERVIEW_SCHEDULED are written by the execution service.
        if state.get("needs_interview"):
            return {**state, "schedule_required": True}
        return state

    def finalize_run(state: dict[str, Any]) -> dict[str, Any]:
        # Terminal node; the engine ends the run (COMPLETED). No side effect here.
        return {**state, "finalized": True}

    return RunGraph(
        nodes=[
            GraphNode("load_application", load_application),
            GraphNode("analyze_candidate", analyze_candidate),
            GraphNode("human_review", human_review),
            GraphNode("update_application_status", update_application_status),
            GraphNode("generate_interview_questions", generate_interview_questions),
            GraphNode("wait_schedule_approval", wait_schedule_approval),
            GraphNode("create_interview_schedule", create_interview_schedule),
            GraphNode("finalize_run", finalize_run),
        ],
        interrupt_after="human_review",
        interrupt_after_conditional={
            # Pause for a second approval only when SHORTLISTED needs a schedule.
            "wait_schedule_approval": lambda s: bool(s.get("needs_interview", False)),
        },
        interrupt_status=RunStatus.WAITING_APPROVAL,
    )
