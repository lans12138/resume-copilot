"""Hermetic tests for the MatchRun route's publication step (FIN-005, §14.4).

The route's contract with the outside world is short and load-bearing: the run is
committed *before* anything is published, and a broker that is down must not turn a
durable run into a 5xx. Both halves are pinned here; the full request path is
exercised end-to-end by ``tests/validate_application_entry.ps1``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.app.agent.enqueuer import CeleryRunEnqueuer
from backend.app.agent.models import AgentRun, RunStatus, RunType
from backend.app.main import create_app
from backend.app.match_run.routes import _publish_match_run
from tests.unit.settings_factory import make_settings


def _run(*, attempt: int = 1) -> AgentRun:
    return AgentRun(
        id=uuid4(),
        thread_id=uuid4().hex,
        run_type=RunType.MATCH,
        status=RunStatus.CREATED,
        attempt=attempt,
        next_event_sequence=0,
        version=1,
        config_snapshot_json={"job_id": str(uuid4())},
        created_at=datetime.now(tz=UTC),
    )


def test_publish_delivers_the_runs_own_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operation key (run + attempt) has to come from the row, not the caller.

    That is what makes a retry's delivery distinguishable from the first pass's,
    and the caller's intent is what lets the worker tell "retry, please" apart from
    a duplicated first-pass message (both arrive against a ``FAILED`` row).
    """
    delivered: list[tuple[UUID, int, Any]] = []

    def _record(
        self: CeleryRunEnqueuer, run_id: UUID, *, attempt: int, intent: Any
    ) -> None:
        delivered.append((run_id, attempt, intent))

    monkeypatch.setattr(CeleryRunEnqueuer, "enqueue_match_run", _record)
    run = _run(attempt=3)

    _publish_match_run(run)

    assert delivered == [(run.id, 3, intent_start())]


def test_publish_forwards_the_retry_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry route re-drives a FAILED run, and only the intent says so.

    ``FAILED`` looks identical to a redelivery of the first pass at the worker, so
    the route must publish the *retry* intent explicitly — never let it collapse to
    the START default, or the worker's claim would refuse the run as already
    terminal and the retry would silently no-op (the CI bug this pins).
    """
    from backend.app.agent.enqueuer import ExecutionIntent

    delivered: list[Any] = []

    def _record(
        self: CeleryRunEnqueuer, run_id: UUID, *, attempt: int, intent: Any
    ) -> None:
        delivered.append(intent)

    monkeypatch.setattr(CeleryRunEnqueuer, "enqueue_match_run", _record)
    run = _run(attempt=2)

    _publish_match_run(run, intent=ExecutionIntent.RETRY)

    assert delivered == [ExecutionIntent.RETRY]


def intent_start() -> Any:
    from backend.app.agent.enqueuer import ExecutionIntent

    return ExecutionIntent.START


def test_publish_failure_does_not_escape_to_the_request(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A Redis outage must leave the run recoverable, not the request failed.

    The run is already durable when this runs, so raising would only report a 5xx
    for a run that unambiguously exists. ``CREATED`` is simultaneously "enqueued"
    and "not yet delivered", which is exactly the state the FIN-006 republish scan
    looks for — hence a warning, never an exception.
    """

    def _explode(
        self: CeleryRunEnqueuer, run_id: UUID, *, attempt: int, intent: Any
    ) -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(CeleryRunEnqueuer, "enqueue_match_run", _explode)
    run = _run()

    with caplog.at_level(logging.WARNING, logger="backend.app.match_run.routes"):
        _publish_match_run(run)  # must not raise

    assert any(str(run.id) in record.getMessage() for record in caplog.records)
    assert run.status is RunStatus.CREATED


def test_celery_enqueuer_uses_the_operation_key_as_task_id() -> None:
    """Delivery identity is observable: same slice, same id; new slice, new id.

    The intent travels as the second task argument so the worker can read it back;
    the first argument plus ``attempt`` form the task id that makes a redelivery
    recognisable.
    """
    sent: list[dict[str, Any]] = []

    class _StubCelery:
        def send_task(self, name: str, **kwargs: Any) -> None:
            sent.append({"name": name, **kwargs})

    enqueuer = CeleryRunEnqueuer(_StubCelery())
    run_id = uuid4()
    enqueuer.enqueue_match_run(run_id, attempt=1, intent=intent_start())
    enqueuer.enqueue_match_run(run_id, attempt=1, intent=intent_start())
    enqueuer.enqueue_match_run(run_id, attempt=2, intent=intent_start())

    assert [item["name"] for item in sent] == ["agent.execute_match_run"] * 3
    assert [item["args"] for item in sent] == [[str(run_id), "start"]] * 3
    task_ids = [item["task_id"] for item in sent]
    assert task_ids[0] == task_ids[1] == f"{run_id}:1"
    assert task_ids[2] == f"{run_id}:2"


def test_async_match_run_endpoints_advertise_202_accepted() -> None:
    """create / retry / cancel all hand execution to a worker, so they must read 202.

    These are not synchronous completions: the request returns the moment the run
    is durable, and the probe (validate_application_entry.ps1) pins that contract by
    requiring a 202 on the retry response. A route that drifts back to the FastAPI
    default of 200 would pass every unit test and still fail that probe — so the
    status code is asserted here, against the OpenAPI schema, where it is local and
    hermetic.
    """
    paths = create_app(make_settings()).openapi()["paths"]
    for path in (
        "/api/v1/jobs/{job_id}/match-runs",
        "/api/v1/match-runs/{run_id}/retry",
        "/api/v1/match-runs/{run_id}/cancel",
    ):
        assert path in paths, f"{path} not registered"
        assert "post" in paths[path], f"{path} is not a POST"
        assert "202" in paths[path]["post"]["responses"], f"{path} must answer 202"
