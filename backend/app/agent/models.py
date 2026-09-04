"""Agent run and event persistence models (IMP-018).

These two tables are the spine of every Agent execution. ``AgentRun`` is the
aggregate root shared by ``MatchRun`` and ``ApplicationRun`` (its ``run_id`` is
the primary key for both), and ``AgentEvent`` is the strictly-ordered audit log
consumed by SSE replay, observability, and recovery.

Concurrency contract for the event sequence (detailed design §4.5): a writer
must lock the run, read and increment ``next_event_sequence`` inside the same
transaction, then insert the event. ``max(sequence)+1`` is forbidden because it
races under concurrent writers. ``AgentRun.next_event_sequence`` is the single
source of truth; the repository layer owns the increment.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database import Base


class RunType(StrEnum):
    """Discriminates the concrete run aggregate owning a row."""

    MATCH = "MATCH"
    APPLICATION = "APPLICATION"


class RunStatus(StrEnum):
    """Lifecycle state of a run, mirrored into AgentEvent.status for UI.

    ``WAITING_APPROVAL`` is the APPLICATION-run state entered when the graph
    pauses at the ``human_review`` node (detailed design §4.5, §11.2). It is
    semantically distinct from ``INTERRUPTED`` (which the generic engine used
    before the ApplicationRun state machine was finalised) and is the value the
    ApplicationRun graph requests via ``RunGraph.interrupt_status``.
    """

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AgentEventType(StrEnum):
    """Structured event kinds written by nodes and the run engine."""

    RUN_CREATED = "RUN_CREATED"
    NODE_STARTED = "NODE_STARTED"
    NODE_COMPLETED = "NODE_COMPLETED"
    RUN_RESUMED = "RUN_RESUMED"
    STATUS_CHANGED = "STATUS_CHANGED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"
    RUN_CANCELLED = "RUN_CANCELLED"


class AgentRun(Base):
    """Aggregate root for one Agent execution (MatchRun or ApplicationRun)."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('CREATED','RUNNING','WAITING_APPROVAL','INTERRUPTED',"
            "'COMPLETED','FAILED','CANCELLED')",
            name="ck_agent_runs_status",
        ),
        CheckConstraint("attempt >= 1", name="ck_agent_runs_attempt"),
        CheckConstraint("next_event_sequence >= 0", name="ck_agent_runs_seq"),
        Index("ix_agent_runs_thread_id", "thread_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # LangGraph checkpointer recovery key; globally unique per run.
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    run_type: Mapped[RunType] = mapped_column(
        Enum(RunType, native_enum=False, create_constraint=False, length=32),
        nullable=False,
    )
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, native_enum=False, create_constraint=False, length=32),
        default=RunStatus.CREATED,
        nullable=False,
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Monotonic counter owned by the repository; never written by callers.
    next_event_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Checkpoint stores only id/config snapshot/routing/bounded results (§17.2).
    config_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Optimistic-lock version for the aggregate root.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Cancellation marker (§5.4/§11.9): set before the run enters CANCELLED.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_by: Mapped[UUID | None] = mapped_column(nullable=True)
    # Failure diagnostics (§4.5/§11.8): a FAILED run is retryable only when set.
    retryable: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message_safe: Mapped[str | None] = mapped_column(String(500))
    failed_node: Mapped[str | None] = mapped_column(String(128))
    # Run snapshot schema version; fixed at creation, bumped on protocol change.
    snapshot_version: Mapped[str] = mapped_column(
        String(64), default="v1", server_default=text("'v1'"), nullable=False
    )
    # Initiating user; nullable to avoid friction with the engine-created run.
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentEvent(Base):
    """One strictly-ordered, business-fact audit entry inside a run."""

    __tablename__ = "agent_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_events_run_sequence"),
        CheckConstraint(
            "event_type IN ('RUN_CREATED','NODE_STARTED','NODE_COMPLETED','RUN_RESUMED',"
            "'STATUS_CHANGED','RUN_COMPLETED','RUN_FAILED','RUN_CANCELLED')",
            name="ck_agent_events_event_type",
        ),
        ForeignKeyConstraint(["run_id"], ["agent_runs.id"], name="fk_agent_events_run"),
        Index("ix_agent_events_run_sequence", "run_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    # Redundant copy for contract validation and read-side filtering.
    run_type: Mapped[RunType] = mapped_column(
        Enum(RunType, native_enum=False, create_constraint=False, length=32), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[AgentEventType] = mapped_column(
        Enum(AgentEventType, native_enum=False, create_constraint=False, length=64),
        nullable=False,
    )
    node: Mapped[str | None] = mapped_column(String(128))
    # Structured UI state; never a natural-language verdict.
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Bounded, never contains full resume text or secrets (§4.5).
    safe_payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CheckpointTuple:
    """Portable snapshot returned by a ``Checkpointer`` (LangGraph-compatible).

    The run engine treats this as the only source of truth for *where the graph
    resumes* after a process restart: ``metadata["next_node"]`` is the node to
    execute next, and ``checkpoint`` is the bounded state to resume with.
    """

    def __init__(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        parent_id: str | None,
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        self.thread_id = thread_id
        self.checkpoint_ns = checkpoint_ns
        self.checkpoint_id = checkpoint_id
        self.parent_id = parent_id
        self.checkpoint = checkpoint
        self.metadata = metadata

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CheckpointTuple):
            return NotImplemented
        return (
            self.thread_id == other.thread_id
            and self.checkpoint_ns == other.checkpoint_ns
            and self.checkpoint_id == other.checkpoint_id
        )

    def __repr__(self) -> str:
        return (
            f"CheckpointTuple(thread_id={self.thread_id!r}, "
            f"checkpoint_id={self.checkpoint_id!r}, next_node={self.metadata.get('next_node')!r})"
        )
