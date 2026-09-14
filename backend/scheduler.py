"""Celery Beat scheduler process entry point (FIN-002).

Run inside the runtime image:

    python backend/scheduler.py

Equivalent to ``celery -A backend.app.infrastructure.celery:app beat``. Emits the
schedules configured in :func:`make_celery_app` (FIN-002: Approval timeout sweep,
plus the FIN-006 schedules once those tasks exist).
"""
from backend.app.infrastructure.celery import app

if __name__ == "__main__":
    app.start(argv=["beat", "--loglevel=info"])
