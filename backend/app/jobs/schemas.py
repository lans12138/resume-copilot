"""Validated Job API schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.auth.models import UserRole
from backend.app.jobs.models import JobStatus


class JobRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_skills: list[str] = Field(default_factory=list, max_length=100)
    preferred_skills: list[str] = Field(default_factory=list, max_length=100)
    minimum_years_experience: float | None = Field(default=None, ge=0, le=100)
    education_level: str | None = Field(default=None, max_length=64)

    @field_validator("required_skills", "preferred_skills")
    @classmethod
    def normalize_skills(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().casefold() for value in values if value.strip()]
        return list(dict.fromkeys(normalized))


class JobCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=50_000)
    requirements: JobRequirements

    @field_validator("title", "description")
    @classmethod
    def strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must contain non-whitespace characters")
        return stripped


class JobUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=50_000)
    requirements: JobRequirements | None = None

    @field_validator("title", "description")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must contain non-whitespace characters")
        return stripped


class VersionCommand(BaseModel):
    expected_version: int = Field(ge=1)


class JobVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version_no: int
    description: str
    requirements: JobRequirements
    content_sha256: str
    schema_version: str
    created_at: datetime


class JobResponse(BaseModel):
    id: UUID
    title: str
    status: JobStatus
    version: int
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    current_version: JobVersionResponse


class JobListResponse(BaseModel):
    items: list[JobResponse]
    page: int
    page_size: int
    total: int


class AssignmentCreate(BaseModel):
    user_id: UUID


class AssignmentResponse(BaseModel):
    id: UUID
    job_id: UUID
    user_id: UUID
    user_role: UserRole = UserRole.HIRING_MANAGER
    assigned_by: UUID
    assigned_at: datetime
    revoked_by: UUID | None
    revoked_at: datetime | None


class AssignmentListResponse(BaseModel):
    items: list[AssignmentResponse]
    page: int
    page_size: int
    total: int
