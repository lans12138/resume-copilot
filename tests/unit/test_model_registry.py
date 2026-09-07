"""Alembic model-discovery regression tests."""

from sqlalchemy import String

from backend.app.infrastructure.model_registry import (
    MIGRATED_MODEL_TABLES,
    get_model_metadata,
)


def test_model_registry_exposes_every_migrated_table() -> None:
    metadata = get_model_metadata()

    assert frozenset(metadata.tables) == MIGRATED_MODEL_TABLES
    assert len(MIGRATED_MODEL_TABLES) == 20


def test_candidate_name_width_matches_the_published_schema() -> None:
    metadata = get_model_metadata()
    display_name_type = metadata.tables["candidates"].c.display_name.type

    assert isinstance(display_name_type, String)
    assert display_name_type.length == 255
