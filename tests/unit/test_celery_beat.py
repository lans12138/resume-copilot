"""FIN-002: Celery app wiring (Beat schedule + task registration)."""
from backend.app.infrastructure.celery import make_celery_app
from tests.unit.settings_factory import make_settings


def test_beat_schedule_registers_expire_approvals() -> None:
    app = make_celery_app(make_settings())

    assert "expire-pending-approvals" in app.conf.beat_schedule
    entry = app.conf.beat_schedule["expire-pending-approvals"]
    assert entry["task"] == "maintenance.expire_approvals"
    assert entry["schedule"] == 60.0


def test_beat_schedule_registers_fin006_sweeps() -> None:
    """FIN-006: the two new sweeps are scheduled, not just implemented.

    A maintenance task that exists but is never emitted is dead code that looks
    alive — the exact failure this asserts against.
    """
    app = make_celery_app(make_settings())

    republish = app.conf.beat_schedule["republish-queued"]
    assert republish["task"] == "maintenance.republish_queued"
    # The interval must exceed the republish grace window, otherwise each cycle
    # would re-publish a row whose original task is still queued.
    assert republish["schedule"] > make_settings().republish_queued_after_seconds

    cleanup = app.conf.beat_schedule["cleanup-orphan-files"]
    assert cleanup["task"] == "maintenance.cleanup_orphan_files"
    assert cleanup["schedule"] == 86_400.0


def test_autodiscover_registers_existing_task_modules() -> None:
    # Importing the task modules registers their @shared_task callbacks on the
    # current app. The Celery worker does this during boot, *before* finalize,
    # so finalize() can bind the tasks to the freshly built app. Without this
    # pre-import, autodiscovery defers module loading until inside finalize(),
    # which is too late for the callbacks to attach to this app instance.
    import backend.app.agent.tasks  # noqa: F401
    import backend.app.candidates.tasks  # noqa: F401
    import backend.app.documents.tasks  # noqa: F401
    import backend.app.evaluations.tasks  # noqa: F401
    import backend.app.maintenance.tasks  # noqa: F401

    app = make_celery_app(make_settings())
    # Finalize triggers task collection without starting a worker.
    app.finalize()

    registered = set(app.tasks.keys())
    assert "agent.execute_match_run" in registered
    assert "agent.execute_application_run" in registered
    assert "maintenance.expire_approvals" in registered
    # FIN-006 tasks must be registered under the names TASK_ROUTES routes.
    assert "maintenance.republish_queued" in registered
    assert "maintenance.cleanup_orphan_files" in registered
    assert "documents.parse" in registered
    # The candidates package's task is named "embeddings.generate_chunks"
    # (per the routing/queue design), not a "candidates.*" prefix.
    assert "embeddings.generate_chunks" in registered
    # FIN-007: the evaluation task must actually be registered, or TASK_ROUTES
    # would route a name no worker can resolve and every submission would sit
    # CREATED forever.
    assert "evaluations.execute" in registered
