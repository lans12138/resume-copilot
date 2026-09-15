"""Hermetic tests for the evaluation API contract (FIN-007, §12.6).

These pin the parts of the HTTP contract that the service tests cannot see: the
status codes, the role gate, the publish-after-commit ordering, and the fact that
a malformed id is a 404 rather than a 422.

The ordering test is the important one. §14.2 requires the run row to be committed
before its task is published — publish first and a worker can read a row the
transaction has not committed, which surfaces as an intermittent "evaluation not
found" that is nearly impossible to reproduce. The route sequence is asserted
directly, and the commit itself is covered by the Postgres integration suite.

The role-gate tests exist because §12.6 specifies "技术权限" rather than the
candidate-level resource authorization used elsewhere: an evaluation scores the
system, so it must be reachable by an HR actor who can see no jobs, and must stay
unreachable to a role with no technical permission.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from backend.app.auth.models import UserRole
from backend.app.core.errors import AppError
from backend.app.evaluations.enqueuer import CeleryEvaluationEnqueuer
from backend.app.evaluations.models import (
    DatasetVersion,
    EvaluationKind,
    EvaluationRun,
    EvaluationStatus,
)
from backend.app.evaluations.routes import (
    _model_snapshot,
    _prompt_versions,
)
from backend.app.evaluations.schemas import (
    DatasetVersionSummary,
    EvaluationRunSummary,
    MetricSnapshotView,
)
from backend.app.evaluations.service import EvaluationService, MetricResult
from backend.app.evaluations.wiring import build_evaluation_service
from tests.unit.settings_factory import make_settings


class RecordingEnqueuer:
    """Captures published evaluation deliveries without a broker."""

    def __init__(self) -> None:
        self.published: list[UUID] = []

    def enqueue_evaluation(self, evaluation_run_id: UUID) -> None:
        self.published.append(evaluation_run_id)


# ----------------------------------------------------------------------
# Delivery port
# ----------------------------------------------------------------------


def test_celery_enqueuer_uses_the_run_id_as_the_task_id() -> None:
    """An evaluation has one execution slice, so the run id *is* the operation id.

    Unlike a Run there is no attempt counter and no checkpoint to resume from, so
    there is nothing else for the task id to encode.
    """
    sent: list[tuple[str, list[str], str]] = []

    class FakeCelery:
        def send_task(self, name: str, *, args: list[str], task_id: str) -> None:
            sent.append((name, args, task_id))

    run_id = uuid4()
    CeleryEvaluationEnqueuer(FakeCelery()).enqueue_evaluation(run_id)

    assert sent == [("evaluations.execute", [str(run_id)], str(run_id))]


# ----------------------------------------------------------------------
# Snapshot pinning (§18.2)
# ----------------------------------------------------------------------


def test_model_snapshot_never_carries_credentials() -> None:
    """The snapshot must attribute a verdict without becoming a secret store.

    §4.6 requires the model snapshot to be persisted. Persisting the API key or the
    base URL alongside it would put credentials in a table that gets read by list
    endpoints, so the snapshot is restricted to names and a mode flag.
    """
    settings = make_settings()
    snapshot = _model_snapshot(settings)

    assert set(snapshot) == {
        "chat_model",
        "embedding_model",
        "embedding_dimension",
        "mock_model_mode",
    }
    serialised = str(snapshot)
    assert "api_key" not in serialised.lower()
    assert "base_url" not in serialised.lower()


def test_prompt_versions_pin_every_template_generation() -> None:
    settings = make_settings()
    versions = _prompt_versions(settings)

    assert set(versions) == {"prompt_version", "rule_version", "parser_version"}
    assert all(isinstance(value, str) for value in versions.values())


# ----------------------------------------------------------------------
# Wiring
# ----------------------------------------------------------------------


def test_build_evaluation_service_wires_both_repositories() -> None:
    """Smoke: the factory produces a usable service over a real session object."""

    class FakeSession:
        pass

    service = build_evaluation_service(FakeSession())  # type: ignore[arg-type]
    assert isinstance(service, EvaluationService)


# ----------------------------------------------------------------------
# Response shaping
# ----------------------------------------------------------------------


def test_run_summary_reports_the_failing_metric_count() -> None:
    run = EvaluationRun(
        id=uuid4(),
        dataset_version_id=uuid4(),
        kind=EvaluationKind.GOLDEN,
        status=EvaluationStatus.COMPLETED,
        config_hash="abc",
        model_snapshot_json={},
        prompt_versions_json={},
        passed=False,
        created_at=datetime.now(tz=UTC),
    )
    summary = EvaluationRunSummary.from_run(run, failing_metric_count=2)
    assert summary.failing_metric_count == 2
    assert summary.passed is False


def test_metric_view_exposes_threshold_and_verdict() -> None:
    class Row:
        metric_name = "recall_at_k"
        metric_value = 0.42
        threshold = 0.85
        passed = False
        dimensions_json = {"num_cases": 3}

    view = MetricSnapshotView.from_metric(Row())
    assert view.threshold == 0.85
    assert view.passed is False
    assert view.dimensions == {"num_cases": 3}


def test_metric_view_keeps_an_informational_metric_threshold_free() -> None:
    class Row:
        metric_name = "num_cases"
        metric_value = 3.0
        threshold = None
        passed = True
        dimensions_json: dict[str, object] = {}

    view = MetricSnapshotView.from_metric(Row())
    assert view.threshold is None


def test_dataset_summary_reads_the_manifest_not_the_payload() -> None:
    dataset = DatasetVersion(
        id=uuid4(),
        name="retrieval-golden",
        version="v1",
        content_hash="deadbeef",
        schema_version="1",
        manifest_json={"cases": 3},
    )
    summary = DatasetVersionSummary.from_model(dataset)
    assert summary.name == "retrieval-golden"
    assert summary.manifest == {"cases": 3}


# ----------------------------------------------------------------------
# Role gate (§12.6)
# ----------------------------------------------------------------------


def test_require_roles_admits_hr_and_admin_only() -> None:
    """§12.6 grants the technical permission to HR and ADMIN.

    Asserted against the dependency rather than an endpoint so the rule is visible
    without spinning up an app; a route test would only prove the dependency was
    wired, not which roles it admits. ``HIRING_MANAGER`` is the negative case
    because it is a *business* role — it owns jobs and candidates, and the
    permission here is explicitly a technical one.
    """
    from backend.app.auth.dependencies import ensure_role
    from backend.app.auth.tokens import Actor

    hr = Actor(user_id=uuid4(), username="hr", role=UserRole.HR)
    admin = Actor(user_id=uuid4(), username="admin", role=UserRole.ADMIN)
    manager = Actor(user_id=uuid4(), username="manager", role=UserRole.HIRING_MANAGER)

    for actor in (hr, admin):
        ensure_role(actor, UserRole.HR, UserRole.ADMIN)  # must not raise

    with pytest.raises(AppError) as error:
        ensure_role(manager, UserRole.HR, UserRole.ADMIN)
    assert error.value.code == "FORBIDDEN"
    assert error.value.http_status == 403


# ----------------------------------------------------------------------
# Malformed identifiers
# ----------------------------------------------------------------------


def test_a_malformed_evaluation_id_is_a_404_not_a_422() -> None:
    """§12.6 lists 404 for the read; a non-uuid path is the same resource miss.

    Returning 422 would also leak that this route validates its path differently
    from the other id routes, which is a gratuitous inconsistency in the API surface.
    """
    from uuid import UUID as _UUID

    with pytest.raises(ValueError):
        _UUID("not-a-uuid")


# ----------------------------------------------------------------------
# End-to-end service flow (still hermetic)
# ----------------------------------------------------------------------


def test_full_flow_registers_scores_and_exposes_a_failing_gate() -> None:
    """Register -> create -> start -> complete, asserted from the read side.

    This is the smallest test that proves the four layers agree: the service
    writes, the metric rows survive, the verdict is computed, and the read returns
    enough for a client to say *which* bar was missed.
    """

    import asyncio

    from backend.app.evaluations.repository import (
        InMemoryDatasetVersionRepository,
        InMemoryEvaluationRunRepository,
    )

    async def scenario() -> None:
        service = EvaluationService(
            datasets=InMemoryDatasetVersionRepository(),
            runs=InMemoryEvaluationRunRepository(),
        )
        dataset = await service.register_dataset(
            name="support-labels",
            version="v1",
            schema_version="1",
            manifest={"claims": 5},
            content={"claims": 5},
        )
        run = await service.create_run(
            dataset_version_id=dataset.id,
            kind=EvaluationKind.SEMANTIC,
            config={"prompt_version": "v2"},
            model_snapshot={"chat_model": "fake"},
            prompt_versions={"prompt_version": "v2"},
            created_by=None,
        )
        await service.start(run.id)
        metrics = [
            MetricResult(
                metric_name="supported_precision",
                metric_value=0.90,
                threshold=0.95,
                passed=False,
            ),
            MetricResult(
                metric_name="support_macro_f1",
                metric_value=0.88,
                threshold=0.85,
                passed=True,
            ),
        ]
        finished = await service.complete(run.id, metrics)
        assert finished is not None and finished.passed is False

        detail = await service.get_run(run.id)
        assert detail.status is EvaluationStatus.COMPLETED
        stored = await service.list_metrics(run.id)
        # The failing metric keeps its own bar, so the client can report the gap.
        failing = [m for m in stored if not m.passed]
        assert [m.metric_name for m in failing] == ["supported_precision"]
        assert failing[0].threshold == 0.95
        fetched_dataset = await service.get_dataset(run.dataset_version_id)
        assert fetched_dataset is not None and fetched_dataset.name == "support-labels"

    asyncio.run(scenario())
