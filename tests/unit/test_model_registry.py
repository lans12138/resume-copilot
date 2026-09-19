"""Alembic model-discovery regression tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from alembic import op
from sqlalchemy import Column, Constraint, PrimaryKeyConstraint, String

from backend.app.explanations.models import MatchExplanation
from backend.app.infrastructure.model_registry import (
    MIGRATED_MODEL_TABLES,
    get_model_metadata,
)
from backend.app.reports.models import ReportClaim


def test_model_registry_exposes_every_migrated_table() -> None:
    metadata = get_model_metadata()

    assert frozenset(metadata.tables) == MIGRATED_MODEL_TABLES
    # 22 workflow tables + FIN-007's dataset_versions / evaluation_runs /
    # metric_snapshots + PORT-003's match_explanations.
    assert len(MIGRATED_MODEL_TABLES) == 26


def test_candidate_name_width_matches_the_published_schema() -> None:
    metadata = get_model_metadata()
    display_name_type = metadata.tables["candidates"].c.display_name.type

    assert isinstance(display_name_type, String)
    assert display_name_type.length == 255


# ----------------------------------------------------------------------
# Migration ↔ model agreement (PORT-003)
# ----------------------------------------------------------------------


def _load_migration(filename: str) -> Any:
    """Import a migration by path; ``versions/`` is not an importable package."""
    path = Path(__file__).resolve().parents[2] / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"_migration_{filename[:-3]}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Recorder:
    """Captures the DDL a migration emits, without a database."""

    def __init__(self) -> None:
        self.columns: dict[str, list[Any]] = {}
        self.constraints: dict[str, list[Any]] = {}
        self.added_columns: dict[str, Any] = {}
        self.indexes: list[tuple[str, str, tuple[str, ...]]] = []
        self.checks: list[tuple[str, str, str]] = []

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> None:
        # ``op.create_table`` takes columns and table-level constraints
        # positionally, in one stream; the two are asserted separately.
        entries = list(args) + list(kwargs.get("constraints", ()))
        self.columns[name] = [e for e in entries if isinstance(e, Column)]
        self.constraints[name] = [e for e in entries if isinstance(e, Constraint)]

    def add_column(self, table: str, column: Any, **_kwargs: Any) -> None:
        self.added_columns[column.name] = column

    def create_index(self, name: str, table: str, columns: Any, **_kwargs: Any) -> None:
        self.indexes.append((name, table, tuple(columns)))

    def create_check_constraint(self, name: str, table: str, condition: str, **_k: Any) -> None:
        self.checks.append((name, table, condition))


@pytest.fixture()
def port003_ddl(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Run ``0013_match_explanations.upgrade()`` against a recording ``op``."""
    recorder = _Recorder()
    for attribute in ("create_table", "add_column", "create_index", "create_check_constraint"):
        monkeypatch.setattr(op, attribute, getattr(recorder, attribute))
    _load_migration("0013_match_explanations.py").upgrade()
    return recorder


def test_migration_creates_exactly_the_models_columns(port003_ddl: _Recorder) -> None:
    """A column added to the model without a migration is a runtime failure.

    The registry test above only pins table *names*, which is enough to catch a
    forgotten ``_LOADED_MODEL_MODULES`` entry and not nearly enough to catch a
    forgotten column.
    """
    declared = set(MatchExplanation.__table__.columns.keys())
    migrated = {column.name for column in port003_ddl.columns["match_explanations"]}
    assert migrated == declared


def test_migration_matches_the_models_nullability(port003_ddl: _Recorder) -> None:
    """Nullability is the contract the service reads: ``summary`` is null exactly
    when nothing was produced, and a NOT NULL there would reject that case."""
    by_name = {column.name: column for column in port003_ddl.columns["match_explanations"]}
    for column in MatchExplanation.__table__.columns:
        migrated = by_name[column.name]
        assert migrated.nullable == column.nullable, column.name


def test_migration_creates_the_models_named_constraints(port003_ddl: _Recorder) -> None:
    """Constraint names are how the migration gate and a later ALTER address them.

    Primary keys are excluded: the ORM leaves them unnamed (SQLAlchemy's own
    convention throughout this codebase) while the migrations name them, so
    comparing those would assert a difference that is intentional.
    """
    table = get_model_metadata().tables["match_explanations"]
    declared = {
        constraint.name
        for constraint in table.constraints
        if constraint.name and not isinstance(constraint, PrimaryKeyConstraint)
    }
    migrated = {
        constraint.name
        for constraint in port003_ddl.constraints["match_explanations"]
        if constraint.name and not isinstance(constraint, PrimaryKeyConstraint)
    }
    assert migrated == declared


def test_migration_adds_the_claim_source_with_a_rule_default(
    port003_ddl: _Recorder,
) -> None:
    """Existing rows must be relabelled by the database, not guessed in Python.

    A wrong ``source`` on a historical claim would relabel a decision as
    commentary, which is the one direction of that mistake that matters.
    """
    added = port003_ddl.added_columns["source"]
    assert added.nullable is False
    assert added.server_default is not None
    assert added.server_default.arg == "RULE"
    assert "source" in ReportClaim.__table__.columns

    assert (
        "ck_report_claims_source",
        "report_claims",
        "source IN ('RULE','MODEL')",
    ) in port003_ddl.checks


def test_migration_indexes_the_run_and_the_status(port003_ddl: _Recorder) -> None:
    """Both are query patterns an operator uses: "what did this run do?" and
    "how many runs are degrading because the upstream is rate limiting us?"."""
    indexed = {(table, columns) for _name, table, columns in port003_ddl.indexes}
    assert ("match_explanations", ("run_id",)) in indexed
    assert ("match_explanations", ("status",)) in indexed


def test_migration_revision_chain_is_pinned(port003_ddl: _Recorder) -> None:
    """The head revision string is what ``validate_migrations.ps1`` asserts, and a
    revision that silently re-points at a different parent would reorder history."""
    module = _load_migration("0013_match_explanations.py")
    assert module.revision == "0013_match_explanations"
    assert module.down_revision == "0012_evaluation_tables"
    assert port003_ddl.columns  # the fixture actually ran the upgrade

