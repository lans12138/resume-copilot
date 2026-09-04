"""Minimal run engine with checkpoint-based interrupt and resume (IMP-018).

``RunEngine`` is intentionally small: it is *not* LangGraph. It models the one
behavior the IMP-018 gate requires — "save and restore the minimal interrupt"
— with a plain ordered node list and a single ``interrupt_after`` node. The
engine writes a structured ``AgentEvent`` for every node start/complete and for
each status transition, and it persists the bounded graph state to a
``Checkpointer`` before pausing. On resume it reads *only* the checkpointer
(metadata["next_node"]) plus the run status to decide where to continue, which
is exactly the recovery contract from detailed design §17.2.

Replacing this engine with a real LangGraph ``StateGraph`` wired to an
``AsyncPostgresSaver`` is a later IMP; the event/checkpoint contracts here stay.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.app.agent.checkpoint import Checkpointer, new_checkpoint_id
from backend.app.agent.models import (
    AgentEventType,
    AgentRun,
    CheckpointTuple,
    RunStatus,
)
from backend.app.agent.repository import AgentRunRepository


class GraphNode:
    """One pure, deterministic step: ``(state) -> state``."""

    def __init__(self, name: str, run: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.name = name
        self._run = run

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        return self._run(dict(state))


class RunGraph:
    """Ordered nodes plus the node after which the engine must pause."""

    def __init__(
        self,
        nodes: list[GraphNode],
        *,

        interrupt_after: str | None = None,
        interrupt_status: RunStatus = RunStatus.INTERRUPTED,
    ) -> None:
        self.nodes = list(nodes)
        self.interrupt_after = interrupt_after
        self.interrupt_status = interrupt_status
        self._validate()

    def _validate(self) -> None:
        names = [n.name for n in self.nodes]
        if len(names) != len(set(names)):
            raise ValueError("graph node names must be unique")
        if self.interrupt_after is not None and self.interrupt_after not in names:
            raise ValueError(f"interrupt_after node {self.interrupt_after!r} not in graph")

    def index_of(self, name: str) -> int:
        for i, node in enumerate(self.nodes):
            if node.name == name:
                return i
        raise ValueError(f"node {name!r} not found")


class InterruptResult:
    """Returned when the engine pauses at the interrupt point."""

    def __init__(
        self, *, run: AgentRun, checkpoint: CheckpointTuple, next_node: str | None
    ) -> None:
        self.run = run
        self.checkpoint = checkpoint
        self.next_node = next_node


class RunEngine:
    """Executes a ``RunGraph`` against a run, emitting ordered events."""

    def __init__(
        self,
        repository: AgentRunRepository,
        checkpointer: Checkpointer,
        *,
        checkpoint_ns: str = "",
    ) -> None:
        self._repository = repository
        self._checkpointer = checkpointer
        self._ns = checkpoint_ns

    async def execute(
        self,
        run: AgentRun,
        graph: RunGraph,
        initial_state: dict[str, Any],
    ) -> InterruptResult | None:
        """Run from the start; pause at ``interrupt_after`` if present."""
        return await self._run_from(run, graph, initial_state, start_index=0, resumed=False)

    async def resume(
        self,
        run: AgentRun,
        graph: RunGraph,
    ) -> InterruptResult | None:
        """Continue a paused run strictly from the checkpoint's next_node."""
        checkpoint = await self._checkpointer.get(run.thread_id, self._ns, "")
        if checkpoint is None:
            raise RuntimeError(f"no checkpoint for thread {run.thread_id}; cannot resume")
        next_node = checkpoint.metadata.get("next_node")
        if next_node is None:
            raise RuntimeError("checkpoint has no next_node; cannot resume")
        if next_node == "__END__":
            return None
        start_index = graph.index_of(next_node)
        await self._repository.append_event(
            run_id=run.id,
            run_type=run.run_type,
            event_type=AgentEventType.RUN_RESUMED,
            node=None,
            status=RunStatus.RUNNING.value,
            message_key="run.resumed",
            safe_payload={"next_node": next_node},
        )
        await self._repository.set_status(run.id, RunStatus.RUNNING)
        return await self._run_from(
            run, graph, checkpoint.checkpoint, start_index=start_index, resumed=True
        )

    async def _run_from(
        self,
        run: AgentRun,
        graph: RunGraph,
        state: dict[str, Any],
        *,
        start_index: int,
        resumed: bool,
    ) -> InterruptResult | None:
        interrupt_index = (
            graph.index_of(graph.interrupt_after) if graph.interrupt_after else -1
        )
        for index in range(start_index, len(graph.nodes)):
            node = graph.nodes[index]
            await self._repository.append_event(
                run_id=run.id,
                run_type=run.run_type,
                event_type=AgentEventType.NODE_STARTED,
                node=node.name,
                status=RunStatus.RUNNING.value,
                message_key=f"node.{node.name}.started",
                safe_payload={},
            )
            state = node.execute(state)
            await self._repository.append_event(
                run_id=run.id,
                run_type=run.run_type,
                event_type=AgentEventType.NODE_COMPLETED,
                node=node.name,
                status=RunStatus.RUNNING.value,
                message_key=f"node.{node.name}.completed",
                safe_payload={"state_keys": sorted(state.keys())},
            )

            if index == interrupt_index and not resumed:
                next_node = (
                    graph.nodes[index + 1].name if index + 1 < len(graph.nodes) else "__END__"
                )
                checkpoint_id = new_checkpoint_id()
                await self._checkpointer.put(
                    run.thread_id,
                    self._ns,
                    checkpoint_id,
                    parent_id=None,
                    checkpoint=state,
                    metadata={"next_node": next_node},
                )
                await self._repository.append_event(
                    run_id=run.id,
                    run_type=run.run_type,
                    event_type=AgentEventType.STATUS_CHANGED,
                    node=node.name,
                    status=graph.interrupt_status.value,
                    message_key="run.interrupted",
                    safe_payload={"checkpoint_id": checkpoint_id, "next_node": next_node},
                )
                await self._repository.set_status(run.id, graph.interrupt_status)
                saved = CheckpointTuple(
                    thread_id=run.thread_id,
                    checkpoint_ns=self._ns,
                    checkpoint_id=checkpoint_id,
                    parent_id=None,
                    checkpoint=state,
                    metadata={"next_node": next_node},
                )
                return InterruptResult(run=run, checkpoint=saved, next_node=next_node)

        await self._repository.append_event(
            run_id=run.id,
            run_type=run.run_type,
            event_type=AgentEventType.RUN_COMPLETED,
            node=None,
            status=RunStatus.COMPLETED.value,
            message_key="run.completed",
            safe_payload={"final_state_keys": sorted(state.keys())},
        )
        await self._repository.set_status(run.id, RunStatus.COMPLETED, finished=True)
        return None
