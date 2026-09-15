"""Evaluation API endpoints (FIN-007, §12.6).

Three routes:

* ``POST /evaluations`` — records the run, commits it, then publishes. Answers
  ``202`` with the run id; ``409 EVALUATION_DUPLICATE`` when an evaluation for the
  same dataset and config is already in flight.
* ``GET /evaluations`` — paginated history with an optional status/kind filter.
* ``GET /evaluations/{id}`` — full verdict: run, dataset, model/prompt pins, and
  the per-metric rows with their thresholds.

**Permission choice — technical permission, not business RBAC.** §12.6 calls for
"HR/ADMIN 技术权限" on submission and a "脱敏技术权限" on reads. Evaluations score
the *system*, not a candidate, so they are not gated by candidate resource
assignment (the ``JobAssignment`` rule): an HR user who can see no jobs may still
need to confirm the gate is green. Both sides therefore require an authenticated
HR or ADMIN actor, and neither filters by assignment. The response never includes
credentials, full resume text, or dataset case payloads, so "脱敏" is satisfied by
construction rather than by a per-row redaction pass.

**Why publish after commit.** The run row is the fact; the task is the delivery
(§14.2). If the publication is lost, the run sits ``CREATED`` and is visible as
stuck — which is strictly better than a request that returns 202 for a run no
worker will ever see, or a worker that reads a row the transaction has not
committed yet.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from backend.app.auth.dependencies import require_roles
from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError
from backend.app.core.settings import Settings
from backend.app.evaluations.enqueuer import EvaluationEnqueuer
from backend.app.evaluations.models import EvaluationKind, EvaluationStatus
from backend.app.evaluations.repository import DEFAULT_PAGE_SIZE
from backend.app.evaluations.schemas import (
    DatasetVersionSummary,
    EvaluationAccepted,
    EvaluationCreateRequest,
    EvaluationDetail,
    EvaluationList,
    EvaluationRunSummary,
    MetricSnapshotView,
)
from backend.app.evaluations.service import EvaluationService
from backend.app.evaluations.wiring import evaluation_enqueuer, evaluation_service

router = APIRouter(prefix="/api/v1/evaluations", tags=["evaluations"])

ServiceDep = Annotated[EvaluationService, Depends(evaluation_service, scope="function")]
# §12.6: submission needs the technical permission (HR or ADMIN). Reads are
# "脱敏技术权限" — same roles, no assignment filter, no sensitive fields. The role
# gate is shared because neither side is scoped to a candidate resource: an
# evaluation scores the *system*, so the JobAssignment rule does not apply.
TechnicalActorDep = Annotated[Actor, Depends(require_roles(UserRole.HR, UserRole.ADMIN))]
ReaderActorDep = Annotated[Actor, Depends(require_roles(UserRole.HR, UserRole.ADMIN))]


def _settings(request: Request) -> Settings:
    """The process settings, read the same way every other route reads them."""
    resolved: Settings = request.app.state.settings
    return resolved


def _model_snapshot(settings: Settings) -> dict[str, object]:
    """Pin which model configuration produced a verdict (§18.2).

    Records the model *names* and whether mock mode was active, never the API key or
    base URL: a verdict must be attributable without the snapshot becoming a place
    credentials leak into the database.
    """
    return {
        "chat_model": settings.chat_model,
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        "mock_model_mode": settings.mock_model_mode,
    }


def _prompt_versions(settings: Settings) -> dict[str, object]:
    """Pin the prompt/rule template versions that shaped the run (§18.2)."""
    return {
        "prompt_version": settings.prompt_version,
        "rule_version": settings.rule_version,
        "parser_version": settings.parser_version,
    }


@router.post("", response_model=EvaluationAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_evaluation(
    body: EvaluationCreateRequest,
    actor: TechnicalActorDep,
    service: ServiceDep,
    enqueuer: Annotated[EvaluationEnqueuer, Depends(evaluation_enqueuer)],
    request: Request = None,  # type: ignore[assignment]
) -> EvaluationAccepted:
    """Register an evaluation run and publish it (§12.6)."""
    settings = _settings(request)
    run = await service.create_run(
        dataset_version_id=body.dataset_version_id,
        kind=body.kind,
        config=body.config,
        model_snapshot=_model_snapshot(settings),
        prompt_versions=_prompt_versions(settings),
        created_by=actor.user_id,
    )
    # The service commits the run (dependency teardown) before the delivery below
    # is published — publishing first would race a worker against an uncommitted row.
    enqueuer.enqueue_evaluation(run.id)
    return EvaluationAccepted(
        evaluation_run_id=run.id,
        status=run.status,
        kind=run.kind,
        dataset_version_id=run.dataset_version_id,
        config_hash=run.config_hash,
    )


@router.get("", response_model=EvaluationList)
async def list_evaluations(
    actor: ReaderActorDep,
    service: ServiceDep,
    status_filter: Annotated[EvaluationStatus | None, Query(alias="status")] = None,
    kind: Annotated[EvaluationKind | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = DEFAULT_PAGE_SIZE,
) -> EvaluationList:
    """Paginated evaluation history, newest first (§12.6)."""
    result = await service.list_page(
        status=status_filter, kind=kind, page=page, page_size=page_size
    )
    items: list[EvaluationRunSummary] = []
    for run in result.items:
        metrics = await service.list_metrics(run.id)
        failing = sum(1 for metric in metrics if metric.threshold is not None and not metric.passed)
        items.append(EvaluationRunSummary.from_run(run, failing_metric_count=failing))
    return EvaluationList(items=items, total=result.total, limit=result.limit, offset=result.offset)


@router.get("/{evaluation_run_id}", response_model=EvaluationDetail)
async def get_evaluation(
    evaluation_run_id: str,
    actor: ReaderActorDep,
    service: ServiceDep,
) -> EvaluationDetail:
    """Full verdict for one run, including per-metric thresholds (§12.6)."""
    try:
        run_id = UUID(evaluation_run_id)
    except ValueError:
        # A malformed id is a 404, not a 422: §12.6 lists 404 as the read error and
        # the resource genuinely does not exist. It also avoids leaking that the
        # path shape is validated differently from the other id routes.
        raise AppError(
            code="EVALUATION_NOT_FOUND",
            http_status=404,
            safe_message="评测不存在",
            details={"evaluation_run_id": evaluation_run_id},
        ) from None

    run = await service.get_run(run_id)
    dataset = await service.get_dataset(run.dataset_version_id)
    metrics = await service.list_metrics(run.id)
    views = [MetricSnapshotView.from_metric(metric) for metric in metrics]
    failing = sum(1 for view in views if view.threshold is not None and not view.passed)
    return EvaluationDetail(
        run=EvaluationRunSummary.from_run(run, failing_metric_count=failing),
        dataset=(
            DatasetVersionSummary.from_model(dataset)
            if dataset is not None
            else DatasetVersionSummary(
                id=run.dataset_version_id,
                name="unknown",
                version="unknown",
                content_hash="",
                schema_version="",
                manifest={},
            )
        ),
        model_snapshot=dict(run.model_snapshot_json),
        prompt_versions=dict(run.prompt_versions_json),
        error_message_safe=run.error_message_safe,
        metrics=views,
    )


__all__ = ["router"]
