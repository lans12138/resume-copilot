"""Checkpointer abstraction (IMP-018).

This module defines the boundary the run engine uses to persist and restore
graph state across an interrupt. The method names deliberately mirror
LangGraph's ``BaseCheckpointSaver`` (``put`` / ``get`` / ``list``) so the
in-memory implementation used for tests and local runs can be swapped for an
``AsyncPostgresSaver`` in IMP-030 without touching ``RunEngine``.

Design rule from detailed design §17.2: the checkpointer decides *where the
graph resumes*, but the business tables are the final source of truth. The
engine stores only a bounded state dict plus ``metadata["next_node"]``; it must
never persist full resume text or secrets.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from backend.app.agent.models import CheckpointTuple


class Checkpointer(Protocol):
    """Async contract for saving and restoring graph checkpoints."""

    async def put(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        parent_id: str | None,
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None: ...

    async def get(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> CheckpointTuple | None: ...

    async def list(self, thread_id: str, checkpoint_ns: str = "") -> list[CheckpointTuple]: ...


def new_checkpoint_id() -> str:
    """Stable-format checkpoint id (mirrors LangGraph's uuid4 hex)."""
    return uuid.uuid4().hex


class InMemoryCheckpointer:
    """Process-local checkpointer; keeps the latest tuple per (thread, ns).

    Good enough for hermetic tests and local runs. The PostgreSQL adapter in a
    later IMP keeps the full history instead of overwriting, which is required
    for multi-branch replay; the engine only ever reads the latest tuple.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], CheckpointTuple] = {}

    async def put(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        parent_id: str | None,
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        self._store[(thread_id, checkpoint_ns)] = CheckpointTuple(
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_id=checkpoint_id,
            parent_id=parent_id,
            checkpoint=dict(checkpoint),
            metadata=dict(metadata),
        )

    async def get(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> CheckpointTuple | None:
        if checkpoint_id:
            found = self._store.get((thread_id, checkpoint_ns))
            if found is not None and found.checkpoint_id == checkpoint_id:
                return found
            return None
        return self._store.get((thread_id, checkpoint_ns))

    async def list(self, thread_id: str, checkpoint_ns: str = "") -> list[CheckpointTuple]:
        found = self._store.get((thread_id, checkpoint_ns))
        return [found] if found is not None else []
