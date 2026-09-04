"""MatchRun persistence ports and adapters (IMP-019).

Two small repositories back the MatchRun aggregate:

* ``MatchRunRepository`` keeps the one-to-one ``MatchRun`` header (job + frozen
  configs). It is write-once in MVP; ``update_match_run`` exists for the retry
  bookkeeping added later (IMP-025).
* ``MatchRunCandidateRepository`` owns the per-candidate snapshot rows. The
  fan-out writes them in two phases: ``save_candidates`` inserts all rows as
  ``PENDING`` during ``snapshot_candidates``; each candidate's fixed node group
  then flips its row to ``COMPLETED`` or ``FAILED``. Both phases go through the
  same in-memory lock so concurrent candidate updates never corrupt the row set.

Two adapters ship: ``InMemoryMatchRunRepository`` / ``InMemoryMatchRunCandidate-
Repository`` for hermetic unit tests and local runs, and the ``Sql*`` variants
for PostgreSQL (exercised end-to-end in IMP-030).
"""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.match_run.models import MatchRun, MatchRunCandidate, ProcessingStatus


class MatchRunRepository(Protocol):
    """Persistence contract for the MatchRun header row."""

    async def save_match_run(self, match_run: MatchRun) -> None: ...

    async def get_match_run(self, run_id: UUID) -> MatchRun | None: ...


class MatchRunCandidateRepository(Protocol):
    """Persistence contract for per-candidate ranking snapshots."""

    async def save_candidates(self, candidates: list[MatchRunCandidate]) -> None: ...

    async def mark_candidate_completed(
        self, *, run_id: UUID, profile_id: UUID, hard_rule_result_json: dict[str, object]
    ) -> None: ...

    async def mark_candidate_failed(
        self, *, run_id: UUID, profile_id: UUID, error_code: str
    ) -> None: ...

    async def get_candidates(self, run_id: UUID) -> list[MatchRunCandidate]: ...


class InMemoryMatchRunRepository:
    """Lock-guarded in-process store for the MatchRun header."""

    def __init__(self) -> None:
        self._runs: dict[UUID, MatchRun] = {}
        self._lock = asyncio.Lock()

    async def save_match_run(self, match_run: MatchRun) -> None:
        async with self._lock:
            self._runs[match_run.run_id] = match_run

    async def get_match_run(self, run_id: UUID) -> MatchRun | None:
        async with self._lock:
            return self._runs.get(run_id)


class InMemoryMatchRunCandidateRepository:
    """Lock-guarded in-process store keyed by ``(run_id, profile_id)``."""

    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, UUID], MatchRunCandidate] = {}
        self._lock = asyncio.Lock()

    async def save_candidates(self, candidates: list[MatchRunCandidate]) -> None:
        async with self._lock:
            for candidate in candidates:
                self._rows[(candidate.run_id, candidate.candidate_profile_id)] = candidate

    async def mark_candidate_completed(
        self, *, run_id: UUID, profile_id: UUID, hard_rule_result_json: dict[str, object]
    ) -> None:
        async with self._lock:
            row = self._require(run_id, profile_id)
            row.processing_status = ProcessingStatus.COMPLETED
            row.hard_rule_result_json = hard_rule_result_json

    async def mark_candidate_failed(
        self, *, run_id: UUID, profile_id: UUID, error_code: str
    ) -> None:
        async with self._lock:
            row = self._require(run_id, profile_id)
            row.processing_status = ProcessingStatus.FAILED
            row.error_code = error_code

    async def get_candidates(self, run_id: UUID) -> list[MatchRunCandidate]:
        async with self._lock:
            return [
                row
                for (rid, _), row in self._rows.items()
                if rid == run_id
            ]

    def _require(self, run_id: UUID, profile_id: UUID) -> MatchRunCandidate:
        row = self._rows.get((run_id, profile_id))
        if row is None:
            raise KeyError(f"no candidate snapshot for run {run_id} profile {profile_id}")
        return row


class SqlMatchRunRepository:
    """PostgreSQL adapter for the MatchRun header."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_match_run(self, match_run: MatchRun) -> None:
        self._session.add(match_run)
        await self._session.flush()

    async def get_match_run(self, run_id: UUID) -> MatchRun | None:
        return await self._session.get(MatchRun, run_id)


class SqlMatchRunCandidateRepository:
    """PostgreSQL adapter for per-candidate snapshots (IMP-030 E2E)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_candidates(self, candidates: list[MatchRunCandidate]) -> None:
        self._session.add_all(candidates)
        await self._session.flush()

    async def mark_candidate_completed(
        self, *, run_id: UUID, profile_id: UUID, hard_rule_result_json: dict[str, object]
    ) -> None:
        row = await self._get(run_id, profile_id)
        row.processing_status = ProcessingStatus.COMPLETED
        row.hard_rule_result_json = hard_rule_result_json
        await self._session.flush()

    async def mark_candidate_failed(
        self, *, run_id: UUID, profile_id: UUID, error_code: str
    ) -> None:
        row = await self._get(run_id, profile_id)
        row.processing_status = ProcessingStatus.FAILED
        row.error_code = error_code
        await self._session.flush()

    async def get_candidates(self, run_id: UUID) -> list[MatchRunCandidate]:
        result = await self._session.execute(
            select(MatchRunCandidate)
            .where(MatchRunCandidate.run_id == run_id)
            .order_by(MatchRunCandidate.snapshot_order)
        )
        return list(result.scalars().all())

    async def _get(self, run_id: UUID, profile_id: UUID) -> MatchRunCandidate:
        result = await self._session.execute(
            select(MatchRunCandidate).where(
                MatchRunCandidate.run_id == run_id,
                MatchRunCandidate.candidate_profile_id == profile_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise KeyError(f"no candidate snapshot for run {run_id} profile {profile_id}")
        return row
