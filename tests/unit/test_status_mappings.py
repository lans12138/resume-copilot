"""Guard the ORM mapping of status columns.

A ``Mapped[SomeStatus]`` annotation does not make SQLAlchemy return an enum: if
the column is declared as a bare ``String(32)``, the ORM hands back a plain
``str`` and mypy cannot tell (it trusts the annotation). That silently broke the
document pipeline on a real database while every hermetic test stayed green,
because the in-memory repositories keep whatever the caller assigned:

- ``documents/tasks.py`` did ``status.value`` on the parsed status and raised
  ``AttributeError: 'str' object has no attribute 'value'``;
- ``ProfileReviewService.confirm_profile`` compares with ``is not``, so a raw
  ``str`` made every confirmation answer 409.

``Enum(..., native_enum=False, create_constraint=False)`` stores the same
VARCHAR(32) (the explicit CHECK constraints still pin the vocabulary) but maps
back to the enum member factually. These tests pin that mapping so the
regression cannot come back silently.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Enum as SaEnum

from backend.app.candidates import schemas as candidate_schemas
from backend.app.candidates.models import CandidateProfile, CandidateProfileStatus
from backend.app.documents.models import DocumentStatus, ResumeDocument

_STATUS_COLUMNS = (
    (ResumeDocument, "status", DocumentStatus),
    (CandidateProfile, "status", CandidateProfileStatus),
)


@pytest.mark.parametrize(("model", "column_name", "expected_enum"), _STATUS_COLUMNS)
def test_status_column_maps_to_enum(
    model: type[object], column_name: str, expected_enum: type
) -> None:
    column_type = model.__table__.c[column_name].type  # type: ignore[attr-defined]
    assert isinstance(column_type, SaEnum), (
        f"{model.__name__}.{column_name} must map to Enum, not {type(column_type).__name__}: "
        "a bare String column returns a raw str and breaks every `.value` caller"
    )
    assert column_type.enum_class is expected_enum


@pytest.mark.parametrize(("model", "column_name", "expected_enum"), _STATUS_COLUMNS)
def test_status_column_keeps_varchar_storage(
    model: type[object], column_name: str, expected_enum: type
) -> None:
    """``native_enum=False`` keeps the existing VARCHAR(32) column, so the
    mapping change needs no migration and no DDL drift."""
    column_type = model.__table__.c[column_name].type  # type: ignore[attr-defined]
    assert column_type.native_enum is False
    assert column_type.create_constraint is False
    assert column_type.length == 32


def test_profile_status_is_one_class_shared_by_orm_and_api() -> None:
    """The ORM and the API schema must share one enum class, not two lookalikes.

    ``candidates/schemas.py`` used to re-declare ``CandidateProfileStatus`` with the
    same name and the same four members as ``candidates/models.py``. Two classes with
    equal values are *equal* but never *identical*, so every identity comparison and
    ``dict[enum, ...]`` lookup across the models/schemas boundary failed while looking
    perfectly correct in a failure report — the pipeline integration test read:

        AssertionError: assert <CandidateProfileStatus.READY: 'READY'> is
                                <CandidateProfileStatus.READY: 'READY'>

    The two definitions also drift silently: adding a member to only one of them
    leaves the other rejecting a value the database accepts. ``documents/schemas.py``
    already imports ``DocumentStatus`` from its models module, so this pins the same
    single-source contract for candidates — on the response model itself, because
    that is the boundary     the API actually exposes.
    """
    assert candidate_schemas.CandidateProfileStatus is CandidateProfileStatus
    assert (
        candidate_schemas.CandidateProfileResponse.model_fields["status"].annotation
        is CandidateProfileStatus
    )
