"""MatchRun persistence models (IMP-019).

``MatchRun`` is the job-level batch analysis aggregate. Its ``run_id`` is the
*primary key* and also the foreign key into ``agent_runs`` — a ``MatchRun`` is a
concrete kind of ``AgentRun`` (detailed design §9). ``MatchRunCandidate`` is the
per-candidate snapshot written during the ``snapshot_candidates`` node: every
channel rank/score, the fused ``rrf_score``, the stable ``snapshot_order``, and
the hard-rule verdict. ``FAIL``/``UNKNOWN`` candidates are *retained* here and
marked ``COMPLETED`` — hiding them is a presentation concern owned by the
candidate list UI, never by retrieval or the run (§8.4, §9.3).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database import Base


class ProcessingStatus(StrEnum):
    """Per-candidate processing outcome inside a MatchRun.

    ``FAILED`` means the candidate's node raised (single-candidate failure
    isolation, detailed design §10.2). A hard-rule ``FAIL``/``UNKNOWN`` is *not*
    a candidate failure: that candidate still reaches ``COMPLETED`` with its
    verdict recorded — it is just not auto-hidden.
    """

    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class MatchRun(Base):
    """Job-level batch-analysis run; one-to-one with an ``AgentRun``."""

    __tablename__ = "match_runs"
    __table_args__ = (
        ForeignKeyConstraint(["run_id"], ["agent_runs.id"], name="fk_match_runs_run"),
        ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_match_runs_job"),
        ForeignKeyConstraint(
            ["job_version_id"], ["job_versions.id"], name="fk_match_runs_job_version"
        ),
        Index("ix_match_runs_job", "job_id"),
    )

    # Primary key *and* the agent_runs foreign key (shared aggregate identity).
    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    job_id: Mapped[UUID] = mapped_column(nullable=False)
    job_version_id: Mapped[UUID] = mapped_column(nullable=False)
    # Frozen at run time so a ranking is fully reproducible (§10.1).
    retrieval_config_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    model_config_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MatchRunCandidate(Base):
    """One candidate's frozen ranking + hard-rule snapshot for a MatchRun."""

    __tablename__ = "match_run_candidates"
    __table_args__ = (
        CheckConstraint(
            "processing_status IN ('PENDING','COMPLETED','FAILED')",
            name="ck_match_run_candidates_status",
        ),
        ForeignKeyConstraint(
            ["run_id"], ["match_runs.run_id"], name="fk_match_run_candidates_run"
        ),
        ForeignKeyConstraint(
            ["candidate_profile_id"],
            ["candidate_profiles.id"],
            name="fk_match_run_candidates_profile",
        ),
        ForeignKeyConstraint(
            ["application_id"],
            ["job_applications.id"],
            name="fk_match_run_candidates_application",
        ),
        UniqueConstraint(
            "run_id", "candidate_profile_id", name="uq_match_run_candidates_profile"
        ),
        UniqueConstraint("run_id", "snapshot_order", name="uq_match_run_candidates_order"),
        Index("ix_match_run_candidates_run", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    candidate_profile_id: Mapped[UUID] = mapped_column(nullable=False)
    application_id: Mapped[UUID] = mapped_column(nullable=False)
    snapshot_order: Mapped[int] = mapped_column(Integer, nullable=False)
    structured_rank: Mapped[int | None] = mapped_column(Integer)
    keyword_rank: Mapped[int | None] = mapped_column(Integer)
    vector_rank: Mapped[int | None] = mapped_column(Integer)
    structured_score: Mapped[float | None] = mapped_column(Numeric(18, 10))
    keyword_score: Mapped[float | None] = mapped_column(Numeric(18, 10))
    vector_score: Mapped[float | None] = mapped_column(Numeric(18, 10))
    rrf_score: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    # PASS/FAIL/UNKNOWN per rule plus the aggregate; present after hard_rule_evaluate.
    hard_rule_result_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        Enum(
            ProcessingStatus,
            native_enum=False,
            create_constraint=False,
            length=32,
        ),
        default=ProcessingStatus.PENDING,
        nullable=False,
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
