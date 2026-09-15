"""Celery worker process entry point (FIN-002).

Run inside the runtime image:

    python backend/worker.py

Consumes every queue declared in ``TASK_ROUTES`` so all task families
(documents, embeddings, agent, evaluations, maintenance) are handled by a
single worker in this MVP deployment. Without an explicit ``-Q`` the worker
only listens on ``task_default_queue`` (documents) and would never pick up
routed tasks such as ``maintenance.expire_approvals``.
"""
from backend.app.infrastructure.celery import TASK_ROUTES, app

# Collect the distinct queue names from the routing table so the worker listens
# on all of them; this stays in sync with TASK_ROUTES automatically.
_QUEUES = ",".join(sorted({route["queue"] for route in TASK_ROUTES.values()}))

if __name__ == "__main__":
    app.worker_main(argv=["worker", "--loglevel=info", "-Q", _QUEUES])
