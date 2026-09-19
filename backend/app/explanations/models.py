"""Persistence for one model explanation call per candidate report (PORT-003).

This row is the *call record*: what was asked of the model, under which versions,
how long it took, how much retry budget it spent, and what happened. It is
deliberately separate from the claims the call produced, because the two answer
different questions:

* the claims answer "what does this candidate's fit look like, and where is that
  written down?" — they belong in the report, next to the deterministic ones;
* this row answers "did the model actually run, and if not, why not?" — a
  question that must still have an answer when the call produced nothing at all.

That second case is why this is a table rather than a key in
``match_reports.model_snapshot_json``. An unavailable explanation is a state an
operator has to be able to query ("how many runs are degrading because the
upstream is rate limiting us?"), and a JSON blob cannot be indexed or aggregated
without a scan. It also gives the failure a home that survives a report with zero
model claims — otherwise "the model never ran" and "the model ran and had nothing
to say" would be indistinguishable.

**Keyed by ``(run_id, application_id)``, not by ``report_id``.** The report
aggregate is rewritten wholesale on a retry (``ReportService.generate_for_run``
deletes a run's reports, claims and evidence), so a foreign key from here to
``match_reports`` would make the explanation table able to block that delete —
turning an unrelated write into a failure, and only in the case where a previous
attempt happened to produce explanations. Mirroring ``match_reports``' own
design, which keeps ``application_id`` a plain uuid column rather than a foreign
key, keeps the two tables independently replaceable. The service always writes a
row for a report it has just read, and deletes the run's rows before writing.

``status``/``reason_code`` are stored as constrained strings rather than native
enums, matching the ``evaluation_runs`` precedent, so a new reason code is a code
change plus a migration and never a silent value.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.infrastructure.database import Base


class ExplanationStatus(StrEnum):
    """Whether an explanation reached the report."""

    SUCCEEDED = "SUCCEEDED"
    UNAVAILABLE = "UNAVAILABLE"


class ExplanationReason(StrEnum):
    """Why an explanation has the status it does.

    One value per distinguishable outcome, so an operator reads the cause off the
    row instead of inferring it from a log line. The transport's retryable /
    permanent split is preserved on purpose: ``RATE_LIMITED``, ``TIMEOUT`` and
    ``UPSTREAM_ERROR`` are transient and a re-run may well succeed, while
    ``SCHEMA_ERROR`` and ``UPSTREAM_REJECTED`` will fail identically forever and
    a retry only burns budget.
    """

    OK = "OK"
    #: Mock mode, or the feature switched off: no call was attempted.
    MODEL_DISABLED = "MODEL_DISABLED"
    #: The candidate has no evidence chunks, so there is nothing to cite.
    NO_EVIDENCE = "NO_EVIDENCE"
    #: HTTP 429, or retries exhausted against it.
    RATE_LIMITED = "RATE_LIMITED"
    #: Connect/read timeout, or retries exhausted against it.
    TIMEOUT = "TIMEOUT"
    #: HTTP 5xx, or a network-level failure, after retries.
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    #: A non-2xx that will not change: bad key, bad request.
    UPSTREAM_REJECTED = "UPSTREAM_REJECTED"
    #: 200 with an unusable envelope, or a reply that violated the schema —
    #: including a conclusion that asked for HIGH impact.
    SCHEMA_ERROR = "SCHEMA_ERROR"
    #: The model replied, but every conclusion it offered was uncitable.
    ILLEGAL_CITATION = "ILLEGAL_CITATION"
    #: An unexpected exception. Recorded so the run still completes.
    INTERNAL_ERROR = "INTERNAL_ERROR"


class MatchExplanation(Base):
    """The model call record for one candidate's report."""

    __tablename__ = "match_explanations"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "application_id",
            name="uq_match_explanations_run_application",
        ),
        Index("ix_match_explanations_run", "run_id"),
        Index("ix_match_explanations_status", "status"),
        CheckConstraint(
            "status IN ('SUCCEEDED','UNAVAILABLE')",
            name="ck_match_explanations_status",
        ),
        CheckConstraint("latency_ms >= 0", name="ck_match_explanations_latency_nonneg"),
        CheckConstraint("attempts >= 0", name="ck_match_explanations_attempts_nonneg"),
        CheckConstraint(
            "conclusion_count >= 0 AND dropped_conclusion_count >= 0 "
            "AND illegal_citation_count >= 0",
            name="ck_match_explanations_counts_nonneg",
        ),
        CheckConstraint(
            "dropped_conclusion_count <= conclusion_count",
            name="ck_match_explanations_dropped_lte_total",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    application_id: Mapped[UUID] = mapped_column(nullable=False)
    candidate_profile_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[ExplanationStatus] = mapped_column(
        Enum(
            ExplanationStatus,
            native_enum=False,
            create_constraint=False,
            length=16,
        ),
        nullable=False,
    )
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The model's own framing of the explanation. Null when nothing was produced.
    #: Kept here rather than as a claim: it is commentary *about* the cited
    #: conclusions, not a decision-relevant assertion of its own.
    summary: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Token usage is recorded when the provider reports it and left null when it
    #: does not — never estimated. A fabricated count would be indistinguishable
    #: from a real one in the metrics that consume this column.
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    conclusion_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dropped_conclusion_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    illegal_citation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "ExplanationReason",
    "ExplanationStatus",
    "MatchExplanation",
]
