"""Service assembly for match explanations (PORT-003).

Mirrors ``match_run.wiring``: the API and the worker must build the explanation
service identically, or a report could carry explanations in one path and not the
other. One builder means the gateway choice, the repository and the on/off switch
cannot drift between them.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.settings import Settings
from backend.app.explanations.gateway import build_explanation_gateway
from backend.app.explanations.repository import SqlExplanationRepository
from backend.app.explanations.service import MatchExplanationService
from backend.app.reports.repository import SqlReportRepository


def build_explanation_service(
    session: AsyncSession, settings: Settings
) -> MatchExplanationService:
    """Assemble the explanation service over one transaction.

    The gateway is built from ``settings`` rather than injected so the mock/real
    choice follows the same single flag the extraction and embedding gateways use.
    """
    return MatchExplanationService(
        repository=SqlExplanationRepository(session),
        # Explanations hang off the same report rows the rule claims live in, so
        # both must be written through one repository over one transaction.
        report_repository=SqlReportRepository(session),
        gateway=build_explanation_gateway(settings),
        enabled=settings.explanation_enabled,
    )


__all__ = ["build_explanation_service"]
