"""Celery application factory for the async document/agent task workers."""
from __future__ import annotations

from celery import Celery  # type: ignore[import-untyped]

from backend.app.core.settings import Settings

# Queue routing per detailed design §14.1.
TASK_ROUTES: dict[str, dict[str, str]] = {
    "documents.parse": {"queue": "documents"},
    "documents.retry_parse": {"queue": "documents"},
    # §7.4: the extraction stage continues the parse pipeline on the same queue.
    "candidates.extract_profile": {"queue": "documents"},
    "embeddings.generate_chunks": {"queue": "embeddings"},
    "agent.execute_match_run": {"queue": "agent"},
    "agent.execute_application_run": {"queue": "agent"},
    "evaluations.execute": {"queue": "evaluations"},
    "maintenance.expire_approvals": {"queue": "maintenance"},
    "maintenance.republish_queued": {"queue": "maintenance"},
    "maintenance.cleanup_orphan_files": {"queue": "maintenance"},
}


def make_celery_app(settings: Settings) -> Celery:
    """Build a configured Celery app; call once per worker/scheduler process."""
    app = Celery("resume_copilot")
    broker = (
        settings.celery_broker_url.get_secret_value()
        if settings.celery_broker_url is not None
        else None
    )
    result_backend = (
        settings.celery_result_backend.get_secret_value()
        if settings.celery_result_backend is not None
        else None
    )
    app.conf.update(
        broker_url=broker,
        result_backend=result_backend,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_time_limit=settings.celery_task_time_limit,
        worker_concurrency=settings.celery_worker_concurrency,
        task_routes=TASK_ROUTES,
        task_default_queue="documents",
        task_default_exchange="resume_copilot",
        task_default_exchange_type="direct",
        # §14.2: acknowledge only after the task finishes. Idempotency is
        # enforced by the database (operation_key + status/attempt guards),
        # never by acks_late alone.
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_acks_on_failure_or_timeout=False,
        worker_prefetch_multiplier=1,
        result_expires=3600,
    )
    # Register task modules so `celery -A backend.app.infrastructure.celery:app`
    # discovers documents.parse / embeddings.generate_chunks etc. without a
    # manual import in the worker entrypoint. Imports are lazy (at finalize).
    app.autodiscover_tasks(
        [
            "backend.app.documents",
            "backend.app.candidates",
            "backend.app.maintenance",
            # FIN-005 / FIN-007 will add tasks.py to these packages. Registering
            # them now keeps autodiscover forward-compatible with no code change
            # once those tasks land.
            "backend.app.agent",
            "backend.app.evaluations",
        ]
    )

    # FIN-002: Celery Beat schedules.
    # - maintenance.expire_approvals runs every minute (Approval timeout sweep, §11.8).
    # - maintenance.republish_queued (every 5 min) and
    #   maintenance.cleanup_orphan_files (daily) are wired by FIN-006 once those
    #   tasks exist; adding them here before then would make beat emit unregistered
    #   tasks and error. They join this dict when FIN-006 lands.
    app.conf.beat_schedule = {
        "expire-pending-approvals": {
            "task": "maintenance.expire_approvals",
            "schedule": 60.0,
        },
    }
    return app


def _build_default_app() -> Celery:
    """Module-level app for `celery -A backend.app.infrastructure.celery:app`.

    A worker/scheduler process always runs with a complete Settings contract,
    so this succeeds there. The fallback only keeps import-time safe when no
    configuration is present (e.g. partial static analysis).
    """
    from backend.app.core.settings import get_settings

    try:
        return make_celery_app(get_settings())
    except Exception:
        return Celery("resume_copilot")


app = _build_default_app()
