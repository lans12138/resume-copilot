"""IMP-010 Candidate/Profile extraction boundary and service tests.

Covers the D08 acceptance matrix: null/enum/date/unknown-field validation at the
draft boundary, model garbage being translated into a stable AppError, the
deterministic FakeModel extractor, and the ProfileExtractionService write path
that persists an untrusted draft as REVIEW_REQUIRED.

No real database is used: repositories are in-memory fakes implementing the same
Protocols the SQLAlchemy adapters implement, matching the project's FakeModel /
pure-logic unit test style.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.candidates.models import Candidate, CandidateProfile, CandidateProfileStatus
from backend.app.candidates.repository import CandidateRepository, CandidateProfileRepository
from backend.app.candidates.schemas import (
    CandidateProfileDraft,
    ContactInfo,
    EducationClaim,
    ExperienceClaim,
    SkillClaim,
    revalidate_draft,
)
from backend.app.candidates.service import ProfileExtractionService
from backend.app.core.errors import AppError
from backend.app.documents.models import DocumentStatus, ResumeDocument
from backend.app.documents.parsers import ParsedDocument
from backend.app.documents.validation import PDF_MEDIA_TYPE
from backend.app.infrastructure.model_gateway import FakeModelGateway, ModelGateway


# --------------------------------------------------------------------------- #
# Draft boundary validation (pure logic, no DB)                               #
# --------------------------------------------------------------------------- #


def test_empty_draft_accepts_all_nulls() -> None:
    # Every field optional: a missing value is a REVIEW_REQUIRED placeholder,
    # never a hard failure at the schema boundary.
    draft = CandidateProfileDraft()
    assert draft.full_name is None
    assert draft.skills == []
    assert draft.unknown_fields == {}


def test_invalid_email_rejected() -> None:
    with pytest.raises(ValidationError):
        ContactInfo(email="not-an-email")


def test_invalid_education_level_rejected() -> None:
    with pytest.raises(ValidationError):
        CandidateProfileDraft(education_level="FOO")


def test_education_level_normalized_to_closed_enum() -> None:
    # Schema accepts the closed English enum only; Chinese labels are mapped to
    # the enum upstream by the model gateway, not here.
    assert CandidateProfileDraft(education_level="bachelor").education_level == "BACHELOR"
    assert CandidateProfileDraft(education_level="  MASTER  ").education_level == "MASTER"


def test_experience_end_before_start_rejected() -> None:
    with pytest.raises(ValidationError):
        ExperienceClaim(
            company="Acme",
            title="Engineer",
            start_date=date(2020, 1, 1),
            end_date=date(2019, 1, 1),
        )


def test_experience_future_end_rejected() -> None:
    with pytest.raises(ValidationError):
        ExperienceClaim(
            company="Acme",
            title="Engineer",
            start_date=date(2020, 1, 1),
            end_date=date(2099, 1, 1),
        )


def test_current_role_skips_end_date_order_check() -> None:
    # Ongoing role: no end_date required, must not raise.
    claim = ExperienceClaim(
        company="Acme",
        title="Engineer",
        start_date=date(2020, 1, 1),
        current=True,
    )
    assert claim.current is True


def test_unknown_fields_rejects_empty_key() -> None:
    with pytest.raises(ValidationError):
        CandidateProfileDraft(unknown_fields={"": "value"})


def test_unknown_fields_rejects_oversized_value() -> None:
    with pytest.raises(ValidationError):
        CandidateProfileDraft(unknown_fields={"x": "a" * 6000})


def test_skill_years_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillClaim(name="python", years=100)


def test_field_too_long_rejected() -> None:
    with pytest.raises(ValidationError):
        CandidateProfileDraft(full_name="张" * 300)


def test_revalidate_draft_wraps_invalid_model_output() -> None:
    # A gateway that bypasses construction validation (model garbage) must be
    # caught at the service boundary and turned into a stable, non-internal error.
    bad = CandidateProfileDraft.model_construct(
        contact=ContactInfo.model_construct(email="not-an-email")
    )
    with pytest.raises(AppError) as error:
        revalidate_draft(bad)
    assert error.value.code == "PROFILE_DRAFT_INVALID"
    assert error.value.http_status == 422


# --------------------------------------------------------------------------- #
# FakeModel extractor (pure logic)                                            #
# --------------------------------------------------------------------------- #


def test_fake_gateway_extracts_skills_contact_education() -> None:
    text = (
        "张三\n"
        "邮箱 zhang@example.com 电话 13800138000\n"
        "技能 Python、FastAPI、PostgreSQL、Redis\n"
        "学历 本科，计算机科学\n"
    )
    parsed = ParsedDocument(media_type=PDF_MEDIA_TYPE, full_text=text, blocks=())

    async def scenario() -> CandidateProfileDraft:
        return await FakeModelGateway().extract_profile(full_text=parsed.full_text, blocks=parsed.blocks)

    draft = asyncio.run(scenario())
    skill_names = {s.name for s in draft.skills}
    assert {"python", "fastapi", "postgresql", "redis"} <= skill_names
    assert draft.contact is not None
    assert draft.contact.email == "zhang@example.com"
    assert draft.education_level == "BACHELOR"
    assert draft.full_name == "张三"


def test_fake_gateway_empty_text_returns_empty_draft() -> None:
    async def scenario() -> CandidateProfileDraft:
        return await FakeModelGateway().extract_profile(full_text="", blocks=())

    draft = asyncio.run(scenario())
    assert draft.full_name is None
    assert draft.skills == []


# --------------------------------------------------------------------------- #
# In-memory fakes for the service write path                                  #
# --------------------------------------------------------------------------- #


class FakeCandidateRepository:
    def __init__(self) -> None:
        self.saved: list[Candidate] = []

    async def save(self, candidate: Candidate) -> None:
        self.saved.append(candidate)

    async def get_by_email_hash(self, email_hash: str | None) -> Candidate | None:
        if email_hash is None:
            return None
        for candidate in self.saved:
            if candidate.normalized_email_hash == email_hash:
                return candidate
        return None


class FakeProfileRepository:
    def __init__(self) -> None:
        self.saved: list[CandidateProfile] = []

    async def save(self, profile: CandidateProfile) -> None:
        self.saved.append(profile)

    async def next_version_no(self, candidate_id: UUID) -> int:
        current = max(
            (p.version_no for p in self.saved if p.candidate_id == candidate_id),
            default=0,
        )
        return current + 1

    async def get(self, profile_id: UUID) -> CandidateProfile | None:
        for profile in self.saved:
            if profile.id == profile_id:
                return profile
        return None


class BadGateway:
    """Model gateway that returns structurally-invalid output."""

    version = "bad-v1"

    async def extract_profile(
        self, *, full_text: str, blocks: Any
    ) -> CandidateProfileDraft:
        return CandidateProfileDraft.model_construct(
            contact=ContactInfo.model_construct(email="not-an-email")
        )


def _hr_actor() -> Actor:
    return Actor(user_id=uuid4(), username="hr", role=UserRole.HR)


def _resume_document() -> ResumeDocument:
    return ResumeDocument(
        id=uuid4(),
        original_filename="resume.pdf",
        storage_key="objects/abc",
        media_type=PDF_MEDIA_TYPE,
        size_bytes=1024,
        content_sha256="a" * 64,
        status=DocumentStatus.PARSING,
        uploaded_by=uuid4(),
    )


def _parsed_resume() -> ParsedDocument:
    return ParsedDocument(
        media_type=PDF_MEDIA_TYPE,
        full_text="李四\n邮箱 li@example.com\n技能 Python、Docker\n学历 硕士\n",
        blocks=(),
    )


# --------------------------------------------------------------------------- #
# Service write path                                                          #
# --------------------------------------------------------------------------- #


def test_service_extract_creates_review_required_profile() -> None:
    candidates = FakeCandidateRepository()
    profiles = FakeProfileRepository()
    service = ProfileExtractionService(candidates, profiles, FakeModelGateway())

    async def scenario() -> CandidateProfile:
        return await service.extract(
            actor=_hr_actor(), document=_resume_document(), parsed=_parsed_resume()
        )

    profile = asyncio.run(scenario())
    # Untrusted output is persisted as REVIEW_REQUIRED, never auto-READY.
    assert profile.status == CandidateProfileStatus.REVIEW_REQUIRED
    assert profile.version_no == 1
    # Skills are normalized (casefold + sorted + deduped).
    assert profile.normalized_skills == ["docker", "python"]
    assert isinstance(profile.profile_json, dict)
    assert candidates.saved  # a Candidate subject was created
    assert profiles.saved[0].id == profile.id


def test_service_extract_rejects_non_hr() -> None:
    service = ProfileExtractionService(
        FakeCandidateRepository(), FakeProfileRepository(), FakeModelGateway()
    )

    async def scenario() -> None:
        actor = Actor(user_id=uuid4(), username="hm", role=UserRole.HIRING_MANAGER)
        await service.extract(actor=actor, document=_resume_document(), parsed=_parsed_resume())

    with pytest.raises(AppError) as error:
        asyncio.run(scenario())
    assert error.value.code == "FORBIDDEN"
    assert error.value.http_status == 403


def test_service_extract_wraps_invalid_model_output() -> None:
    service = ProfileExtractionService(
        FakeCandidateRepository(), FakeProfileRepository(), BadGateway()  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        await service.extract(
            actor=_hr_actor(), document=_resume_document(), parsed=_parsed_resume()
        )

    with pytest.raises(AppError) as error:
        asyncio.run(scenario())
    assert error.value.code == "PROFILE_DRAFT_INVALID"
    assert error.value.http_status == 422


def test_service_next_version_no_increments_on_redelivery() -> None:
    profiles = FakeProfileRepository()
    candidate_id = uuid4()

    async def scenario() -> tuple[int, int]:
        first = await profiles.next_version_no(candidate_id)
        profiles.saved.append(
            CandidateProfile(
                candidate_id=candidate_id,
                document_id=uuid4(),
                version_no=first,
                status=CandidateProfileStatus.REVIEW_REQUIRED,
                profile_json={},
                normalized_skills=[],
                schema_version="v1",
            )
        )
        second = await profiles.next_version_no(candidate_id)
        return first, second

    first, second = asyncio.run(scenario())
    assert first == 1
    assert second == 2


def test_service_get_profile_not_found() -> None:
    service = ProfileExtractionService(
        FakeCandidateRepository(), FakeProfileRepository(), FakeModelGateway()
    )

    async def scenario() -> None:
        await service.get_profile(uuid4())

    with pytest.raises(AppError) as error:
        asyncio.run(scenario())
    assert error.value.code == "PROFILE_NOT_FOUND"
    assert error.value.http_status == 404
