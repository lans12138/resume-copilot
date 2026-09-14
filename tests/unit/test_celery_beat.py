"""FIN-002: Celery app wiring (Beat schedule + task registration)."""
from backend.app.infrastructure.celery import make_celery_app
from tests.unit.settings_factory import make_settings


def test_beat_schedule_registers_expire_approvals() -> None:
    app = make_celery_app(make_settings())

    assert "expire-pending-approvals" in app.conf.beat_schedule
    entry = app.conf.beat_schedule["expire-pending-approvals"]
    assert entry["task"] == "maintenance.expire_approvals"
    assert entry["schedule"] == 60.0


def test_autodiscover_registers_existing_task_modules() -> None:
    # Importing the task modules registers their @shared_task callbacks on the
    # current app. The Celery worker does this during boot, *before* finalize,
    # so finalize() can bind the tasks to the freshly built app. Without this
    # pre-import, autodiscovery defers module loading until inside finalize(),
    # which is too late for the callbacks to attach to this app instance.
    import backend.app.candidates.tasks  # noqa: F401
    import backend.app.documents.tasks  # noqa: F401
    import backend.app.maintenance.tasks  # noqa: F401

    app = make_celery_app(make_settings())
    # Finalize triggers task collection without starting a worker.
    app.finalize()

    registered = set(app.tasks.keys())
    assert "maintenance.expire_approvals" in registered
    assert "documents.parse" in registered
    # The candidates package's task is named "embeddings.generate_chunks"
    # (per the routing/queue design), not a "candidates.*" prefix.
    assert "embeddings.generate_chunks" in registered
