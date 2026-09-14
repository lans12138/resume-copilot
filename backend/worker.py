"""Celery worker process entry point (FIN-002).

Run inside the runtime image:

    python backend/worker.py

Equivalent to ``celery -A backend.app.infrastructure.celery:app worker`` but kept
as an explicit, importable entry point that the Compose ``worker`` service invokes.
The app's autodiscover configuration registers every existing task module.
"""
from backend.app.infrastructure.celery import app

if __name__ == "__main__":
    app.worker_main(argv=["worker", "--loglevel=info"])
