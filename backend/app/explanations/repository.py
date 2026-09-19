"""Explanation persistence ports and adapters (PORT-003).

Same shape as the report repository: a lock-guarded in-memory store and a
PostgreSQL adapter behind one protocol, so the service under test and the service
in production take identical code paths.
"""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.explanations.models import MatchExplanation


class ExplanationRepository(Protocol):
    """Persistence contract for explanation call records."""

    async def save(self, explanation: MatchExplanation) -> None: ...

    async def delete_by_run(self, run_id: UUID) -> None: ...

    async def list_by_run(self, run_id: UUID) -> list[MatchExplanation]: ...


class InMemoryExplanationRepository:
    """Lock-guarded in-process store keyed by ``(run_id, application_id)``."""

    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, UUID], MatchExplanation] = {}
        self._lock = asyncio.Lock()

    async def save(self, explanation: MatchExplanation) -> None:
        async with self._lock:
            # Upsert on the natural key, mirroring the unique constraint: a second
            # write for the same report replaces rather than duplicates.
            self._rows[(explanation.run_id, explanation.application_id)] = explanation

    async def delete_by_run(self, run_id: UUID) -> None:
        async with self._lock:
            self._rows = {
                key: row for key, row in self._rows.items() if key[0] != run_id
            }

    async def list_by_run(self, run_id: UUID) -> list[MatchExplanation]:
        async with self._lock:
            rows = [row for key, row in self._rows.items() if key[0] == run_id]
        rows.sort(key=lambda row: str(row.candidate_profile_id))
        return rows


class SqlExplanationRepository:
    """PostgreSQL adapter."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, explanation: MatchExplanation) -> None:
        self._session.add(explanation)
        await self._session.flush()

    async def delete_by_run(self, run_id: UUID) -> None:
        """Drop a run's explanation rows before it is re-explained (§5.6).

        Independent of the report tables on purpose: the explanation record is
        keyed by ``(run_id, application_id)`` rather than by ``report_id``, so a
        report rewrite can never be blocked by an explanation row and this delete
        can never be skipped by one.
        """
        await self._session.execute(
            delete(MatchExplanation).where(MatchExplanation.run_id == run_id)
        )
        await self._session.flush()

    async def list_by_run(self, run_id: UUID) -> list[MatchExplanation]:
        rows = list(
            await self._session.scalars(
                select(MatchExplanation)
                .where(MatchExplanation.run_id == run_id)
                .order_by(MatchExplanation.candidate_profile_id)
            )
        )
        return rows


__all__ = [
    "ExplanationRepository",
    "InMemoryExplanationRepository",
    "SqlExplanationRepository",
]
