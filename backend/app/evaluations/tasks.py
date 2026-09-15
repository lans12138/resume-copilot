"""Celery task: ``evaluations.execute`` (FIN-007, §14.1).

A thin wrapper in the §14 precedent: it parses parameters, builds the worker's
resources, and delegates to :mod:`backend.app.evaluations.service`. All lifecycle
logic — the claim, the metric write, the terminal transition — lives in the
service so it is testable without a broker.

Two properties this task must preserve:

* **The claim comes first (``CREATED`` -> ``RUNNING``).** Evaluation is
  at-least-once like every other task, so a duplicate delivery must find the run
  already advanced and do nothing rather than score it twice. ``start`` returns
  ``None`` in that case, and the task reports ``skipped``.
* **A failed threshold is not a failure.** A run that completes with
  ``passed=False`` is recorded as ``COMPLETED``; only a scorer that could not run
  produces ``FAILED``. The task distinguishes the two rather than letting an
  exception collapse them.

Async note (identical to the maintenance/agent tasks): the SQLAlchemy engine and
redis client are loop-bound and Celery's prefork worker forks after import, so
resources are built per invocation and disposed inside the *same* ``asyncio.run``.
Neither a cached ``RuntimeResources`` nor a second ``asyncio.run`` is safe here.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from celery import Task, shared_task  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.settings import Settings, get_settings
from backend.app.evaluations.executor import BuiltinEvaluationExecutor
from backend.app.evaluations.repository import (
    SqlDatasetVersionRepository,
    SqlEvaluationRunRepository,
)
from backend.app.evaluations.service import EvaluationService
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.retrieval.models import RetrievalConfig

logger = logging.getLogger(__name__)

# §20.4 FIN-007 error code written when the harness itself could not run.
EVALUATION_EXECUTION_FAILED = "EVALUATION_EXECUTION_FAILED"


def build_executor(settings: Settings) -> BuiltinEvaluationExecutor:
    """The built-in hermetic suites, bound to the configured retrieval knobs.

    Reads the retrieval config from ``Settings`` rather than hard-coding it so an
    evaluation scored under one weight set is attributed to that set (the config
    hash recorded on the run changes when these change).
    """
    return BuiltinEvaluationExecutor(retrieval_config=RetrievalConfig.from_settings(settings))


def _build_service(session: AsyncSession) -> EvaluationService:
    """Assemble the evaluation service over one transaction."""
    return EvaluationService(
        datasets=SqlDatasetVersionRepository(session),
        runs=SqlEvaluationRunRepository(session),
    )


async def _execute(
    resources: RuntimeResources,
    session: AsyncSession,
    settings: Settings,
    evaluation_run_id: UUID,
) -> dict[str, object]:
    service = _build_service(session)
    run = await service.start(evaluation_run_id)
    if run is None:
        # Already claimed, already finished, or never existed. All three are
        # expected under at-least-once delivery; none is an error.
        return {"status": "skipped", "evaluation_run_id": str(evaluation_run_id)}

    executor = build_executor(settings)
    try:
        metrics = executor.run(run.kind)
    except Exception as error:  # noqa: BLE001 - harness boundary: record, don't crash
        logger.exception("evaluations.execute scorer failed run_id=%s", evaluation_run_id)
        await service.fail(
            evaluation_run_id,
            error_code=EVALUATION_EXECUTION_FAILED,
            safe_message="评测执行失败，请查看服务日志",
        )
        return {
            "status": "failed",
            "evaluation_run_id": str(evaluation_run_id),
            "error_code": EVALUATION_EXECUTION_FAILED,
            "reason": type(error).__name__,
        }

    finished = await service.complete(evaluation_run_id, metrics)
    if finished is None:
        # A concurrent worker won the terminal transition. Its verdict stands.
        return {"status": "skipped", "evaluation_run_id": str(evaluation_run_id)}
    failing = [m.metric_name for m in metrics if m.threshold is not None and not m.passed]
    return {
        "status": "ok",
        "evaluation_run_id": str(evaluation_run_id),
        # ``passed`` is the gate verdict; a False here is a *successful run that
        # failed its bar*, which is exactly the signal CI reacts to.
        "passed": bool(finished.passed),
        "metrics": len(metrics),
        "failing": failing,
    }


@shared_task(name="evaluations.execute", bind=True)  # type: ignore[untyped-decorator]
def execute_evaluation(self: Task, evaluation_run_id: str) -> dict[str, object]:
    """Score one evaluation run and persist its metric snapshots (§14.1)."""
    settings = get_settings()
    resources = RuntimeResources.build(settings)

    async def _run() -> dict[str, object]:
        session = resources.session_factory()
        try:
            result = await _execute(resources, session, settings, UUID(evaluation_run_id))
            await session.commit()
            return result
        finally:
            await session.close()
            # Tear the loop-bound engine/redis down in the same loop that built
            # them, or the next task's loop inherits a foreign-bound connection.
            await resources.close()

    return asyncio.run(_run())


__all__ = ["EVALUATION_EXECUTION_FAILED", "execute_evaluation"]
