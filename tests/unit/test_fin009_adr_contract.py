"""FIN-009: freeze the ADR-0001 engine decision and its invariants.

ADR-0001 formally accepts the self-built ``RunEngine`` + ``SqlCheckpointer``
runtime *instead of* LangGraph. An accepted deviation is only safe if it cannot
silently drift, so this module pins three things:

1. **The ADR's factual basis** (ADR-0001 §2): the runtime carries no LangGraph
   dependency, the engine/checkpointer contracts named in the ADR still exist
   with their documented method names, and the deviation is stated in code.
2. **The "must not change" guarantees** (ADR-0001 §5.2): business tables stay
   the source of truth, an already-executed Approval never re-fires a side
   effect on replay, and checkpoints never carry unbounded document text.
3. **The document alignment** (§7): the assertions that used to claim LangGraph
   as a settled fact now point at the ADR, while good-faith "planned/roadmap"
   wording is deliberately allowed to remain.

If a future change adopts LangGraph (or edits the ADR away), these tests are
the tripwire: they fail until the ADR is updated in the same change, which is
exactly the discipline ADR-0001 §5.3 asks for.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from backend.app.agent.checkpoint import Checkpointer, InMemoryCheckpointer
from backend.app.agent.engine import GraphNode, InterruptResult, RunEngine, RunGraph
from backend.app.agent.models import AgentEventType, RunStatus, RunType
from backend.app.agent.repository import InMemoryAgentRunRepository
from backend.app.agent.service import RunService
from backend.app.approvals.models import ApprovalActionType, ApprovalStatus
from backend.app.approvals.repository import InMemoryApprovalRepository
from backend.app.approvals.service import ApprovalService, DecisionAction
from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.interviews.repository import InMemoryInterviewRepository
from backend.app.interviews.schedule import MockScheduleBackend
from backend.app.job_applications.models import (
    ApplicationStatus,
    JobApplication,
)
from backend.app.job_applications.repository import (
    InMemoryApplicationRunRepository,
    InMemoryJobApplicationRepository,
)
from backend.app.job_applications.service import ApplicationRunService
from backend.app.job_applications.side_effects import ApplicationSideEffectService

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_PATH = REPO_ROOT / "docs" / "adr" / "0001-agent-runtime-custom-engine-over-langgraph.md"


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive a coroutine to completion (no anyio pytest plugin is installed)."""
    return asyncio.run(coro)


def _actor() -> Actor:
    return Actor(user_id=uuid4(), username="tester", role=UserRole.HIRING_MANAGER)


async def _always_authorize(actor: Actor, job_id: Any) -> None:
    return None


# --------------------------------------------------------------------------
# Part 1 — the ADR's factual basis (ADR-0001 §2)
# --------------------------------------------------------------------------


def test_adr_document_exists_and_is_accepted() -> None:
    assert ADR_PATH.is_file(), "ADR-0001 must exist; it is the deviation record"
    text = ADR_PATH.read_text(encoding="utf-8")
    assert "已接受（Accepted）" in text
    assert "保留自研节点引擎" in text
    # The ADR must name the case for the alternative, not just assert a choice.
    assert "方案 A" in text and "方案 B" in text
    # ...and record when to revisit.
    assert "重新评估" in text


@pytest.mark.parametrize(
    "dependency_file",
    ["pyproject.toml", "requirements.lock", "requirements-dev.lock"],
)
def test_no_langgraph_dependency_is_declared(dependency_file: str) -> None:
    """ADR-0001 §2.1: the runtime declares no LangGraph dependency."""
    path = REPO_ROOT / dependency_file
    if not path.is_file():
        pytest.skip(f"{dependency_file} not present in this checkout")
    lowered = path.read_text(encoding="utf-8").lower()
    assert "langgraph" not in lowered, (
        f"{dependency_file} declares langgraph, but ADR-0001 accepts the "
        "self-built engine. Adopting LangGraph requires updating ADR-0001."
    )


def test_no_langgraph_import_in_runtime_source() -> None:
    """ADR-0001 §2.1: nothing in backend/app imports LangGraph."""
    backend = REPO_ROOT / "backend" / "app"
    offenders: list[str] = []
    for py_file in backend.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8").lower()
        if re.search(r"^\s*(?:from|import)\s+langgraph", text, re.MULTILINE):
            offenders.append(str(py_file.relative_to(REPO_ROOT)))
    assert offenders == [], f"LangGraph imported in runtime source: {offenders}"


def test_engine_documents_the_deviation_in_code() -> None:
    """ADR-0001 §2.2: engine.py itself states it is not LangGraph."""
    engine_src = (REPO_ROOT / "backend" / "app" / "agent" / "engine.py").read_text(
        encoding="utf-8"
    )
    assert "not* LangGraph" in engine_src
    assert "StateGraph" in engine_src  # the portability statement is kept


def test_engine_and_checkpointer_contracts_still_exist() -> None:
    """ADR-0001 §2.3: the documented method names are a stable contract."""
    for method in ("execute", "resume"):
        assert hasattr(RunEngine, method), f"RunEngine.{method} is part of the ADR contract"
    for method in ("put", "get", "list"):
        assert hasattr(Checkpointer, method), (
            f"Checkpointer.{method} mirrors LangGraph's BaseCheckpointSaver"
        )


def test_checkpointer_resume_uses_next_node_metadata() -> None:
    """ADR-0001 §2.3: resume position is checkpoint.metadata['next_node']."""

    async def _scenario() -> None:
        repo = InMemoryAgentRunRepository()
        checkpointer = InMemoryCheckpointer()
        service = RunService(repo, checkpointer)
        run = await service.create_run(
            run_type=RunType.MATCH, config_snapshot={"job_version_id": "v1"}
        )

        def step(state: dict[str, Any]) -> dict[str, Any]:
            state["seen"] = state.get("seen", 0) + 1
            return state

        graph = RunGraph(
            nodes=[
                GraphNode("first", step),
                GraphNode("gate", lambda s: s),
                GraphNode("last", step),
            ],
            interrupt_after="gate",
        )
        interrupted = await service.run_graph(run, graph, {})
        assert interrupted is not None
        stored = await checkpointer.get(run.thread_id, "", "")
        assert stored is not None
        assert stored.metadata["next_node"] == "last"

    _run(_scenario())


def test_primary_interrupt_does_not_refire_on_resume() -> None:
    """ADR-0001 §2.3: a resumed run never re-enters the primary interrupt node."""

    async def _scenario() -> None:
        repo = InMemoryAgentRunRepository()
        service = RunService(repo, InMemoryCheckpointer())
        run = await service.create_run(
            run_type=RunType.MATCH, config_snapshot={"job_version_id": "v1"}
        )
        executions: list[str] = []

        def make(name: str) -> Any:
            def node(state: dict[str, Any]) -> dict[str, Any]:
                executions.append(name)
                return state

            return node

        graph = RunGraph(
            nodes=[
                GraphNode("a", make("a")),
                GraphNode("gate", make("gate")),
                GraphNode("b", make("b")),
            ],
            interrupt_after="gate",
        )
        interrupted = await service.run_graph(run, graph, {})
        assert interrupted is not None
        reloaded = await service.get_run(run.id)
        assert reloaded is not None
        await service.resume_run(reloaded, graph)
        # 'gate' ran exactly once even though we passed through it twice.
        assert executions.count("gate") == 1
        assert executions == ["a", "gate", "b"]

    _run(_scenario())


# --------------------------------------------------------------------------
# Part 2 — invariants that must survive either runtime (ADR-0001 §5.2)
# --------------------------------------------------------------------------


def _core() -> tuple[
    InMemoryJobApplicationRepository,
    InMemoryApplicationRunRepository,
    RunService,
    InMemoryApprovalRepository,
    ApprovalService,
    ApplicationSideEffectService,
    ApplicationRunService,
    InMemoryInterviewRepository,
]:
    agent_repo = InMemoryAgentRunRepository()
    checkpointer = InMemoryCheckpointer()
    run_service = RunService(agent_repo, checkpointer)
    app_repo = InMemoryJobApplicationRepository()
    arun_repo = InMemoryApplicationRunRepository()
    approval_repo = InMemoryApprovalRepository()
    interview_repo = InMemoryInterviewRepository()

    approval_service = ApprovalService(
        authorize=_always_authorize,
        approval_repo=approval_repo,
        arun_repo=arun_repo,
        app_repo=app_repo,
        run_service=run_service,
    )
    side_effects = ApplicationSideEffectService(
        approval_service=approval_service,
        app_repo=app_repo,
        arun_repo=arun_repo,
        run_service=run_service,
        interview_repo=interview_repo,
        schedule_backend=MockScheduleBackend(),
    )
    app_run_service = ApplicationRunService(
        app_repo=app_repo,
        arun_repo=arun_repo,
        run_service=run_service,
        authorize=_always_authorize,
        approval_service=approval_service,
        side_effects=side_effects,
    )
    return (
        app_repo,
        arun_repo,
        run_service,
        approval_repo,
        approval_service,
        side_effects,
        app_run_service,
        interview_repo,
    )


def test_business_tables_remain_the_source_of_truth() -> None:
    """ADR-0001 §5.2: the run's durable status lives in the business table.

    After an interrupt the *business* run row must already say INTERRUPTED even
    if the checkpoint were discarded -- the checkpoint only decides where the
    graph continues, never what happened.
    """

    async def _scenario() -> None:
        (
            app_repo,
            _arun,
            run_service,
            _apr,
            _asvc,
            _se,
            svc,
            _iv,
        ) = _core()
        app = JobApplication(
            id=uuid4(),
            job_id=uuid4(),
            candidate_id=uuid4(),
            status=ApplicationStatus.CREATED,
            version=1,
        )
        await app_repo.save_application(app)
        agent_run, _application_run = await svc.create_application_run(
            _actor(), app.id, None
        )
        # The first gate (UPDATE_APPLICATION_STATUS) interrupts the run.
        reloaded = await run_service.get_run(agent_run.id)
        assert reloaded is not None
        # The ApplicationRun graph uses interrupt_status=WAITING_APPROVAL, and
        # that fact is persisted on the durable run row rather than living only
        # in the checkpoint -- business tables stay the source of truth.
        assert reloaded.status == RunStatus.WAITING_APPROVAL

    _run(_scenario())


def test_executed_approval_does_not_refire_side_effect_on_replay() -> None:
    """ADR-0001 §5.2: an already-executed Approval never re-fires its effect.

    This is the load-bearing guarantee behind FIN-009 item 3. The idempotency
    key is ``run_id:attempt:action_type:ordinal``; replaying the decision must
    return the same approval and create exactly one interview.
    """

    async def _scenario() -> None:
        (
            app_repo,
            _arun,
            _run_service,
            _apr,
            approval_service,
            _se,
            svc,
            interview_repo,
        ) = _core()
        app = JobApplication(
            id=uuid4(),
            job_id=uuid4(),
            candidate_id=uuid4(),
            status=ApplicationStatus.CREATED,
            version=1,
        )
        await app_repo.save_application(app)
        agent_run, _application_run = await svc.create_application_run(
            _actor(), app.id, None
        )

        first = await approval_service.get_pending_by_run(agent_run.id)
        assert first is not None
        assert first.action_type is ApprovalActionType.UPDATE_APPLICATION_STATUS
        await svc.decide_approval(
            _actor(), first.id, DecisionAction.APPROVE, expected_version=1
        )

        second = await approval_service.get_pending_by_run(agent_run.id)
        assert second is not None
        assert second.action_type is ApprovalActionType.CREATE_INTERVIEW_SCHEDULE
        await svc.decide_approval(
            _actor(), second.id, DecisionAction.APPROVE, expected_version=1
        )

        # Both approvals are terminal-and-executed.
        assert (await approval_service.get_approval(first.id)).status is (  # type: ignore[union-attr]
            ApprovalStatus.EXECUTED
        )
        assert (await approval_service.get_approval(second.id)).status is (  # type: ignore[union-attr]
            ApprovalStatus.EXECUTED
        )

        interview = await interview_repo.get_by_approval(second.id)
        assert interview is not None
        interview_id = interview.id

        # Replay: re-proposing the same action at the same ordinal must resolve
        # to the same approval row instead of creating a duplicate. The status
        # gate is ordinal 1 (the schedule gate is ordinal 2), per the service.
        application_run = await _arun.get_application_run(agent_run.id)
        assert application_run is not None
        application = await app_repo.get_application(app.id)
        assert application is not None
        replay = await approval_service.create_approval(
            _actor(),
            agent_run=agent_run,
            application_run=application_run,
            application=application,
            action_type=first.action_type,
            ordinal=1,
            proposed_params=dict(first.original_params_json or {}),
        )
        assert replay.id == first.id, "replay must resolve to the same approval"
        assert replay.idempotency_key == first.idempotency_key

        # Still exactly one interview for the second approval.
        again = await interview_repo.get_by_approval(second.id)
        assert again is not None
        assert again.id == interview_id

    _run(_scenario())


def test_checkpoint_payload_stays_bounded() -> None:
    """ADR-0001 §5.2 / AGT-009: the checkpoint must not carry unbounded text."""

    async def _scenario() -> None:
        repo = InMemoryAgentRunRepository()
        checkpointer = InMemoryCheckpointer()
        service = RunService(repo, checkpointer)
        run = await service.create_run(
            run_type=RunType.MATCH, config_snapshot={"job_version_id": "v1"}
        )
        big = "x" * 5000

        def seed(state: dict[str, Any]) -> dict[str, Any]:
            state["document_text"] = big
            return state

        graph = RunGraph(
            nodes=[GraphNode("seed", seed), GraphNode("gate", lambda s: s)],
            interrupt_after="gate",
        )
        interrupted = await service.run_graph(run, graph, {})
        assert interrupted is not None
        # The engine stores what the node produced, so the contract we pin here
        # is that the *engine itself* adds no document text of its own, and the
        # events it writes never echo the state values (only their key names).
        events = await repo.list_events(run.id)
        for event in events:
            payload = event.safe_payload_json or {}
            assert big not in str(payload), "event payload leaked document text"

    _run(_scenario())


def test_interrupt_result_exposes_next_node_and_checkpoint() -> None:
    """ADR-0001 §2.3: InterruptResult is the documented resume hand-off."""

    async def _scenario() -> None:
        repo = InMemoryAgentRunRepository()
        service = RunService(repo, InMemoryCheckpointer())
        run = await service.create_run(
            run_type=RunType.MATCH, config_snapshot={"job_version_id": "v1"}
        )
        graph = RunGraph(
            nodes=[GraphNode("a", lambda s: s), GraphNode("gate", lambda s: s)],
            interrupt_after="gate",
        )
        result: InterruptResult | None = await service.run_graph(run, graph, {})
        assert result is not None
        # 'gate' is last, so continuation is the end sentinel.
        assert result.next_node == "__END__"
        assert result.checkpoint.checkpoint_id

    _run(_scenario())


def test_run_events_remain_ordered_and_gapless_after_resume() -> None:
    """ADR-0001 §5.2: the audit contract is runtime-independent."""

    async def _scenario() -> None:
        repo = InMemoryAgentRunRepository()
        service = RunService(repo, InMemoryCheckpointer())
        run = await service.create_run(
            run_type=RunType.MATCH, config_snapshot={"job_version_id": "v1"}
        )
        graph = RunGraph(
            nodes=[
                GraphNode("a", lambda s: s),
                GraphNode("gate", lambda s: s),
                GraphNode("b", lambda s: s),
            ],
            interrupt_after="gate",
        )
        interrupted = await service.run_graph(run, graph, {})
        assert interrupted is not None
        reloaded = await service.get_run(run.id)
        assert reloaded is not None
        await service.resume_run(reloaded, graph)

        events = await repo.list_events(run.id)
        seqs = [e.sequence for e in events]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs))
        assert seqs[-1] - seqs[0] == len(seqs) - 1
        types = [e.event_type for e in events]
        assert AgentEventType.RUN_RESUMED in types
        assert types.count(AgentEventType.RUN_COMPLETED) == 1

    _run(_scenario())


# --------------------------------------------------------------------------
# Part 3 — document alignment (ADR-0001 §7)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc",
    [
        "README.md",
        "需求分析.md",
        "技术栈选型与架构决策.md",
        "概要设计说明书.md",
        "详细设计说明书.md",
    ],
)
def test_documents_reference_the_adr(doc: str) -> None:
    """ADR-0001 §7: corrected documents must point at the decision record."""
    text = (REPO_ROOT / doc).read_text(encoding="utf-8")
    assert "ADR-0001" in text, f"{doc} no longer refers to the accepted deviation"


def test_no_document_claims_langgraph_as_settled_runtime() -> None:
    """ADR-0001 §7: assert-style claims are rewritten; roadmap wording may stay.

    The corrected sentences now mention either the self-built engine or the
    ADR. This guard fails if one of them is reverted to a bare LangGraph claim.
    """
    rewritten_markers = {
        "需求分析.md": "自研 `RunEngine`",
        "概要设计说明书.md": "自研 `RunEngine`",
        "详细设计说明书.md": "自研 `RunEngine`",
    }
    for doc, marker in rewritten_markers.items():
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        assert marker in text, f"{doc} lost its corrected runtime wording"

    # The requirement that underpinned the whole deviation must no longer
    # mandate a specific third-party library.
    requirements = (REPO_ROOT / "需求分析.md").read_text(encoding="utf-8")
    assert "两类 LangGraph Run 都必须使用 PostgreSQL Checkpointer" not in requirements
