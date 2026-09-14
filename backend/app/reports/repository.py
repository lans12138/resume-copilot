"""Report persistence ports and adapters (IMP-020).

The repository stores the three report tables independently (no ORM
relationships) and assembles the read-side ``ReportView`` hierarchy on read so
the in-memory and SQL adapters are interchangeable. ``list_by_run`` returns the
claims in ``display_order`` and each claim's evidences in a stable
(claim_id, chunk_id, range) order, matching §4.5 ordering requirements.
"""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.reports.models import (
    ClaimEvidence,
    ClaimView,
    MatchReport,
    ReportClaim,
    ReportView,
)


class ReportRepository(Protocol):
    """Persistence contract for evidence-backed MatchRun reports."""

    async def save_report(self, report: MatchReport) -> None: ...

    async def save_claim(self, claim: ReportClaim) -> None: ...

    async def save_evidence(self, evidence: ClaimEvidence) -> None: ...

    async def delete_by_run(self, run_id: UUID) -> None: ...

    async def list_by_run(self, run_id: UUID) -> list[ReportView]: ...


class InMemoryReportRepository:
    """Lock-guarded in-process store assembling ``ReportView`` on read."""

    def __init__(self) -> None:
        self._reports: dict[UUID, MatchReport] = {}
        self._claims: dict[UUID, ReportClaim] = {}
        self._evidences: list[ClaimEvidence] = []
        self._lock = asyncio.Lock()

    async def save_report(self, report: MatchReport) -> None:
        async with self._lock:
            self._reports[report.id] = report

    async def save_claim(self, claim: ReportClaim) -> None:
        async with self._lock:
            self._claims[claim.id] = claim

    async def save_evidence(self, evidence: ClaimEvidence) -> None:
        async with self._lock:
            self._evidences.append(evidence)

    async def delete_by_run(self, run_id: UUID) -> None:
        async with self._lock:
            doomed = {r.id for r in self._reports.values() if r.run_id == run_id}
            self._reports = {
                rid: report for rid, report in self._reports.items() if rid not in doomed
            }
            self._claims = {
                cid: claim for cid, claim in self._claims.items() if claim.report_id not in doomed
            }
            self._evidences = [
                e for e in self._evidences if e.claim_id in self._claims
            ]

    async def list_by_run(self, run_id: UUID) -> list[ReportView]:
        async with self._lock:
            return self._assemble(run_id)

    def _assemble(self, run_id: UUID) -> list[ReportView]:
        reports = [
            report for report in self._reports.values() if report.run_id == run_id
        ]
        views: list[ReportView] = []
        for report in reports:
            claim_rows = sorted(
                (c for c in self._claims.values() if c.report_id == report.id),
                key=lambda c: c.display_order,
            )
            claim_views: list[ClaimView] = []
            for claim in claim_rows:
                evs = sorted(
                    (e for e in self._evidences if e.claim_id == claim.id),
                    key=lambda e: (str(e.evidence_chunk_id), e.quote_start, e.quote_end),
                )
                claim_views.append(ClaimView(claim=claim, evidences=list(evs)))
            views.append(ReportView(report=report, claims=claim_views))
        views.sort(key=lambda v: v.report.candidate_profile_id)
        return views


class SqlReportRepository:
    """PostgreSQL adapter; assembled on read via ordered queries (IMP-030 E2E)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_report(self, report: MatchReport) -> None:
        self._session.add(report)
        await self._session.flush()

    async def save_claim(self, claim: ReportClaim) -> None:
        self._session.add(claim)
        await self._session.flush()

    async def save_evidence(self, evidence: ClaimEvidence) -> None:
        self._session.add(evidence)
        await self._session.flush()

    async def delete_by_run(self, run_id: UUID) -> None:
        """Drop a run's reports together with their claims and evidence (§5.6).

        Reports are *derived* data — a retry re-scores the run and must not leave
        two attempts' conclusions side by side under one run. Children go first:
        the foreign keys declare no ON DELETE CASCADE, and relying on one would
        hide the ordering requirement from the reader.
        """
        report_ids = select(MatchReport.id).where(MatchReport.run_id == run_id)
        claim_ids = select(ReportClaim.id).where(ReportClaim.report_id.in_(report_ids))
        await self._session.execute(
            delete(ClaimEvidence).where(ClaimEvidence.claim_id.in_(claim_ids))
        )
        await self._session.execute(
            delete(ReportClaim).where(ReportClaim.report_id.in_(report_ids))
        )
        await self._session.execute(delete(MatchReport).where(MatchReport.run_id == run_id))
        await self._session.flush()

    async def list_by_run(self, run_id: UUID) -> list[ReportView]:
        reports = list(
            await self._session.scalars(
                select(MatchReport).where(MatchReport.run_id == run_id)
            )
        )
        views: list[ReportView] = []
        for report in reports:
            claim_rows = list(
                await self._session.scalars(
                    select(ReportClaim)
                    .where(ReportClaim.report_id == report.id)
                    .order_by(ReportClaim.display_order)
                )
            )
            claim_views: list[ClaimView] = []
            for claim in claim_rows:
                ev_rows = list(
                    await self._session.scalars(
                        select(ClaimEvidence).where(ClaimEvidence.claim_id == claim.id)
                    )
                )
                evs = sorted(
                    ev_rows,
                    key=lambda e: (str(e.evidence_chunk_id), e.quote_start, e.quote_end),
                )
                claim_views.append(ClaimView(claim=claim, evidences=evs))
            views.append(ReportView(report=report, claims=claim_views))
        views.sort(key=lambda v: v.report.candidate_profile_id)
        return views
