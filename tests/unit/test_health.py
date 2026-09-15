"""Readiness aggregation and local Storage probe tests."""

import asyncio
from pathlib import Path

import pytest

from backend.app.core.health import check_readiness
from backend.app.infrastructure.storage import check_storage
from tests.unit.fake_resources import make_fake_resources


def test_readiness_reports_each_core_dependency() -> None:
    ready, dependencies = asyncio.run(check_readiness(make_fake_resources()))

    assert ready is True
    assert dependencies == {
        "postgres": {"status": "up"},
        "redis": {"status": "up"},
        "storage": {"status": "up"},
    }


def test_readiness_marks_only_the_failed_dependency_down() -> None:
    ready, dependencies = asyncio.run(
        check_readiness(make_fake_resources("redis"))
    )

    assert ready is False
    assert dependencies["postgres"] == {"status": "up"}
    assert dependencies["redis"] == {"status": "down"}
    assert dependencies["storage"] == {"status": "up"}


def test_storage_probe_reads_writes_and_removes_temporary_file(tmp_path: Path) -> None:
    check_storage(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_storage_probe_rejects_a_missing_mount(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing"

    with pytest.raises(RuntimeError, match="not an existing directory"):
        check_storage(missing_root)
