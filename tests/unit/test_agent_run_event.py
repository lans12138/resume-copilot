"""IMP-018 tests: ordered events, concurrent sequence, and checkpoint resume.

These run fully in-memory (no database, no model backend) so the gate stays
hermetic. The same repository/checkpointer contracts back the PostgreSQL
adapters shipped in this IMP.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from backend.app.agent.checkpoint import InMemoryCheckpointer
from backend.app.agent.engine import GraphNode, RunGraph
from backend.app.agent.models import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    RunStatus,
    RunType,
)
from backend.app.agent.repository import InMemoryAgentRunRepository
from backend.app.agent.service import RunService


def _graph() -> RunGraph:
    """load_inputs -> compute_scores -> [interrupt] await_review -> finalize."""

    def load(state: dict[str, object]) -> dict[str, object]:
        state["loaded"] = True
        return state

    def compute(state: dict[str, object]) -> dict[str, object]:
        state["score"] = 42
        return state

    def finalize(state: dict[str, object]) -> dict[str, object]:
        state["report_written"] = True
        return state

    return RunGraph(
        nodes=[
            GraphNode("load_inputs", load),
            GraphNode("compute_scores", compute),
            GraphNode("await_review", lambda s: s),
            GraphNode("finalize", finalize),
        ],
        interrupt_after="await_review",
    )


async def _build_run(service: RunService) -> AgentRun:
    return await service.create_run(
        run_type=RunType.MATCH,
        config_snapshot={"job_version_id": "v1", "top_k": 10},
    )


async def _run_full(repo: InMemoryAgentRunRepository, run: AgentRun) -> None:
    service = RunService(repo, InMemoryCheckpointer())
    graph = _graph()
    interrupt = await service.run_graph(run, graph, {})
    assert interrupt is not None
    reloaded = await service.get_run(run.id)
    assert reloaded is not None
    await service.resume_run(reloaded, graph)


def test_execute_pauses_at_interrupt_with_ordered_events() -> None:
    async def _run() -> None:
        repo = InMemoryAgentRunRepository()
        service = RunService(repo, InMemoryCheckpointer())
        run = await _build_run(service)
        graph = _graph()

        result = await service.run_graph(run, graph, {"seed": 1})

        assert result is not None
        assert result.next_node == "finalize"
        assert run.status == RunStatus.INTERRUPTED

        run2 = await service.get_run(run.id)
        assert run2 is not None and run2.status == RunStatus.INTERRUPTED

        events = await service.list_events(run.id)
        # RUN_CREATED + (started,completed)x3 + STATUS_CHANGED(interrupted) = 8
        assert [e.sequence for e in events] == list(range(8))
        assert events[0].event_type == AgentEventType.RUN_CREATED
        assert events[1].event_type == AgentEventType.NODE_STARTED
        assert events[1].node == "load_inputs"
        assert events[2].event_type == AgentEventType.NODE_COMPLETED
        assert events[6].node == "await_review"
        assert events[7].event_type == AgentEventType.STATUS_CHANGED
        assert events[7].status == RunStatus.INTERRUPTED.value
        assert all(e.run_type == RunType.MATCH for e in events)

    asyncio.run(_run())


def test_resume_continues_from_checkpoint_and_completes() -> None:
    async def _run() -> None:
        repo = InMemoryAgentRunRepository()
        service = RunService(repo, InMemoryCheckpointer())
        run = await _build_run(service)
        graph = _graph()

        interrupt = await service.run_graph(run, graph, {"seed": 1})
        assert interrupt is not None

        # Simulate a process restart: only the repository + checkpointer survive.
        reloaded = await service.get_run(run.id)
        assert reloaded is not None
        result = await service.resume_run(reloaded, graph)

        assert result is None  # ran to completion, no second interrupt
        finished = await service.get_run(run.id)
        assert finished is not None and finished.status == RunStatus.COMPLETED

        events = await service.list_events(run.id)
        # sequence stays strictly contiguous across the restart.
        assert [e.sequence for e in events] == list(range(len(events)))
        resumed_event = next(e for e in events if e.event_type == AgentEventType.RUN_RESUMED)
        assert resumed_event.status == RunStatus.RUNNING.value
        completed = events[-1]
        assert completed.event_type == AgentEventType.RUN_COMPLETED
        assert completed.status == RunStatus.COMPLETED.value

    asyncio.run(_run())


def test_checkpoint_state_survives_restart() -> None:
    async def _run() -> None:
        repo = InMemoryAgentRunRepository()
        service = RunService(repo, InMemoryCheckpointer())
        run = await _build_run(service)
        graph = _graph()

        await service.run_graph(run, graph, {"seed": 7})
        reloaded = await service.get_run(run.id)
        assert reloaded is not None
        await service.resume_run(reloaded, graph)

        all_events = await service.list_events(run.id)
        completed = all_events[-1]
        assert completed.safe_payload_json.get("final_state_keys") == [
            "loaded",
            "report_written",
            "score",
            "seed",
        ]

    asyncio.run(_run())


def test_concurrent_append_keeps_sequence_contiguous_no_duplicates() -> None:
    async def _run() -> None:
        repo = InMemoryAgentRunRepository()
        await repo.save_run(
            AgentRun(
                id=UUID(int=1),
                thread_id="thread-concurrent",
                run_type=RunType.MATCH,
                status=RunStatus.RUNNING,
                next_event_sequence=0,
                version=1,
                config_snapshot_json={},
            )
        )

        async def writer(_: int) -> AgentEvent:
            return await repo.append_event(
                run_id=UUID(int=1),
                run_type=RunType.MATCH,
                event_type=AgentEventType.NODE_COMPLETED,
                node="n",
                status=RunStatus.RUNNING.value,
                message_key="m",
                safe_payload={},
            )

        events = await asyncio.gather(*[writer(i) for i in range(200)])

        sequences = sorted(e.sequence for e in events)
        assert sequences == list(range(200))
        assert len({e.sequence for e in events}) == 200  # no duplicates

    asyncio.run(_run())


def test_list_events_ordered_by_sequence() -> None:
    async def _run() -> None:
        repo = InMemoryAgentRunRepository()
        run = await _build_run(RunService(repo, InMemoryCheckpointer()))
        await _run_full(repo, run)

        events = await repo.list_events(run.id)
        assert all(
            events[i].sequence < events[i + 1].sequence for i in range(len(events) - 1)
        )

    asyncio.run(_run())
