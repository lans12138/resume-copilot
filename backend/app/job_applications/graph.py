"""ApplicationRun graph definition (IMP-021).

Fixed graph (detailed design §11.2), trimmed to the part IMP-021 owns:

    load_application → analyze_candidate (no side effects) → human_review [interrupt]

``human_review`` pauses the run at ``WAITING_APPROVAL`` and keeps the active slot
occupied until a human decision (IMP-022) resumes it. The side-effect nodes
(``update_application_status`` / ``propose_schedule`` / ``create_interview_schedule``)
are added in IMP-022/024 *behind* an Approval; their absence here is deliberate —
an ApplicationRun must never call a mutation before a human approves it.

Every node is a pure ``(state) -> state`` transform so the engine can replay and
interrupt deterministically (§17.2). The proposal built by ``analyze_candidate``
is typed data only; it carries no model-asserted verdict and no resume text.
"""

from __future__ import annotations

from typing import Any

from backend.app.agent.engine import GraphNode, RunGraph
from backend.app.agent.models import RunStatus


def build_application_graph() -> RunGraph:
    """Return the IMP-021 ApplicationRun graph (interrupt at human_review)."""

    def load_application(state: dict[str, Any]) -> dict[str, Any]:
        # Pure read: pin the application context the later nodes need. The
        # caller has already injected job_id / candidate_id / current_status.
        return {**state, "application_loaded": True}

    def analyze_candidate(state: dict[str, Any]) -> dict[str, Any]:
        # No side effects. Build a typed proposal the human will review. The
        # target status defaults to SHORTLISTED; IMP-022/024 refine the schema.
        proposal = {
            "action_type": "UPDATE_APPLICATION_STATUS",
            "original_params": {
                "target_status": state.get("proposed_status", "SHORTLISTED"),
            },
        }
        return {**state, "proposal": proposal, "proposal_ready": True}

    def human_review(state: dict[str, Any]) -> dict[str, Any]:
        # Freeze the proposal for the pending approval (created in IMP-022).
        return {**state, "pending_approval": state.get("proposal")}

    return RunGraph(
        nodes=[
            GraphNode("load_application", load_application),
            GraphNode("analyze_candidate", analyze_candidate),
            GraphNode("human_review", human_review),
        ],
        interrupt_after="human_review",
        interrupt_status=RunStatus.WAITING_APPROVAL,
    )
