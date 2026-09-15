"""Pure Job schema, version hash, lifecycle, and role-policy tests."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError
from backend.app.jobs.models import JobStatus
from backend.app.jobs.schemas import JobCreate, JobRequirements
from backend.app.jobs.service import content_hash, require_hr, require_transition


def test_job_requirements_normalize_and_deduplicate_skills() -> None:
    requirements = JobRequirements(
        required_skills=[" Python ", "PYTHON", "PostgreSQL"],
        preferred_skills=[" Docker "],
        minimum_years_experience=3,
    )

    assert requirements.required_skills == ["python", "postgresql"]
    assert requirements.preferred_skills == ["docker"]


def test_job_create_rejects_blank_content_and_unknown_requirement_fields() -> None:
    with pytest.raises(ValidationError):
        JobCreate.model_validate(
            {
                "title": "   ",
                "description": "description",
                "requirements": {"untrusted_control": "ignored?"},
            }
        )


def test_content_hash_is_canonical_and_changes_with_business_content() -> None:
    first = JobRequirements(required_skills=["python", "sql"])
    equivalent = JobRequirements(required_skills=[" python ", "SQL"])
    different = JobRequirements(required_skills=["python"])

    assert content_hash("Backend role", first) == content_hash("Backend role", equivalent)
    assert content_hash("Backend role", first) != content_hash("Backend role", different)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (JobStatus.DRAFT, JobStatus.ACTIVE),
        (JobStatus.ACTIVE, JobStatus.CLOSED),
    ],
)
def test_allowed_job_transitions(current: JobStatus, target: JobStatus) -> None:
    require_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (JobStatus.DRAFT, JobStatus.CLOSED),
        (JobStatus.ACTIVE, JobStatus.DRAFT),
        (JobStatus.CLOSED, JobStatus.ACTIVE),
    ],
)
def test_invalid_job_transitions_return_conflict(
    current: JobStatus,
    target: JobStatus,
) -> None:
    with pytest.raises(AppError) as error:
        require_transition(current, target)

    assert error.value.code == "INVALID_STATE"
    assert error.value.http_status == 409


def test_only_hr_has_job_management_role() -> None:
    hr = Actor(user_id=uuid4(), username="hr", role=UserRole.HR)
    manager = Actor(user_id=uuid4(), username="manager", role=UserRole.HIRING_MANAGER)

    require_hr(hr)
    with pytest.raises(AppError) as error:
        require_hr(manager)

    assert error.value.code == "FORBIDDEN"
