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
    app = make_celery_app(make_settings())
    # Finalize triggers task collection without starting a worker.
    app.finalize()

    registered = set(app.tasks.keys())
    assert "maintenance.expire_approvals" in registered
    assert any(name.startswith("documents.") for name in registered)
    assert any(name.startswith("candidates.") for name in registered)
