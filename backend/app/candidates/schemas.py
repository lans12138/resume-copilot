"""Candidate profile draft schema produced by the model extraction gateway.

The draft is the boundary contract between untrusted model output and the
trusted domain. Pydantic validators reject malformed output (over-long fields,
illegal enums, inverted date ranges, future dates) so the service layer can
treat a successfully built ``CandidateProfileDraft`` as already normalized.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Re-exported, not redeclared: the ORM owns this enum. A second
# ``class CandidateProfileStatus(StrEnum)`` with the same four members used to live
# here, and two classes with equal values are never ``is``-identical — so identity
# comparisons and ``dict[enum, ...]`` lookups across the ORM/API boundary failed
# while printing identically. The ``as`` alias marks the deliberate re-export for
# mypy strict; ``documents/schemas.py`` imports its status enum from the models
# module for the same reason.
from backend.app.candidates.models import CandidateProfileStatus as CandidateProfileStatus
from backend.app.core.errors import AppError
from backend.app.retrieval.models import HardRuleBundle
from backend.app.retrieval.preview import CandidateRankingRow, CandidateRankingView

# Education levels are a closed, normalized enum. Anything else is treated as
# model garbage and rejected at the draft boundary.
EDUCATION_LEVELS: tuple[str, ...] = (
    "OTHER",
    "HIGH_SCHOOL",
    "ASSOCIATE",
    "BACHELOR",
    "MASTER",
    "PHD",
)

_MAX_NAME = 200
_MAX_SKILL = 80
_MAX_ITEM_TEXT = 5_000
_MAX_BLOCKS = 100
_MAX_UNKNOWN_KEYS = 50


class ContactInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=64)

    @field_validator("email")
    @classmethod
    def email_has_at_symbol(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "@" not in value or value.strip() != value or " " in value:
            raise ValueError("email must contain a single @ and no surrounding whitespace")
        return value.strip().lower()


class SkillClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=_MAX_SKILL)
    years: float | None = Field(default=None, ge=0, le=60)


class _DateRangeItem(BaseModel):
    """Shared date-range validation for experience and education claims."""

    model_config = ConfigDict(extra="forbid")

    start_date: date | None = None
    end_date: date | None = None
    current: bool = False
    description: str | None = Field(default=None, max_length=_MAX_ITEM_TEXT)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def parse_iso_date(cls, value: Any) -> date | None:
        if value is None or isinstance(value, date):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                return date.fromisoformat(stripped)
            except ValueError as error:
                raise ValueError("date must be ISO format YYYY-MM-DD") from error
        raise ValueError("date must be ISO format YYYY-MM-DD")

    @model_validator(mode="after")
    def check_date_order(self) -> Self:
        # `current` means the role is ongoing; an end date is not yet known.
        if (
            self.end_date is not None
            and not self.current
            and self.start_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("end_date must not be earlier than start_date")
        if self.end_date is not None and self.end_date > date.today():
            raise ValueError("end_date must not be in the future")
        return self


class ExperienceClaim(_DateRangeItem):
    company: str = Field(min_length=1, max_length=_MAX_NAME)
    title: str = Field(min_length=1, max_length=_MAX_NAME)


class EducationClaim(_DateRangeItem):
    school: str = Field(min_length=1, max_length=_MAX_NAME)
    degree: str | None = Field(default=None, max_length=_MAX_NAME)
    major: str | None = Field(default=None, max_length=_MAX_NAME)


class ProjectClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=_MAX_NAME)
    role: str | None = Field(default=None, max_length=_MAX_NAME)
    description: str | None = Field(default=None, max_length=_MAX_ITEM_TEXT)
    url: str | None = Field(default=None, max_length=2048)


class CandidateProfileDraft(BaseModel):
    """Structured candidate profile returned by the extraction gateway.

    Every field is optional on purpose: the source document is untrusted, so a
    missing value is acceptable and becomes a REVIEW_REQUIRED placeholder for a
    human to complete. Unknown model output is captured explicitly in
    ``unknown_fields`` instead of silently widening the schema.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, max_length=_MAX_NAME)
    contact: ContactInfo | None = None
    skills: list[SkillClaim] = Field(default_factory=list, max_length=_MAX_BLOCKS)
    experiences: list[ExperienceClaim] = Field(default_factory=list, max_length=_MAX_BLOCKS)
    education: list[EducationClaim] = Field(default_factory=list, max_length=_MAX_BLOCKS)
    projects: list[ProjectClaim] = Field(default_factory=list, max_length=_MAX_BLOCKS)
    education_level: str | None = None
    unknown_fields: dict[str, Any] = Field(default_factory=dict, max_length=_MAX_UNKNOWN_KEYS)

    @field_validator("education_level")
    @classmethod
    def education_level_is_closed_enum(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in EDUCATION_LEVELS:
            raise ValueError(
                "education_level must be one of: " + ", ".join(EDUCATION_LEVELS)
            )
        return normalized

    @field_validator("unknown_fields")
    @classmethod
    def unknown_fields_not_too_deep(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError("unknown_fields keys must be non-empty strings")
            # Keep the captured blob bounded and JSON-safe.
            if len(repr(item)) > _MAX_ITEM_TEXT:
                raise ValueError("unknown_fields values must be small")
        return value


class CandidateProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    candidate_id: UUID
    document_id: UUID
    version_no: int
    status: CandidateProfileStatus
    profile_json: dict[str, Any]
    normalized_skills: list[str]
    years_experience: float | None
    education_level: str | None
    schema_version: str
    confirmed_by: UUID | None
    confirmed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int


def revalidate_draft(draft: CandidateProfileDraft) -> CandidateProfileDraft:
    """Re-parse a draft as untrusted input at the service boundary.

    The gateway may already validate, but the service must not trust it. A
    raised ``ValidationError`` is translated into a stable ``AppError`` so the
    caller can surface a bounded, non-internal failure.
    """
    try:
        return CandidateProfileDraft.model_validate(draft.model_dump(mode="json"))
    except Exception as error:  # pydantic ValidationError is the expected case
        raise AppError(
            code="PROFILE_DRAFT_INVALID",
            http_status=422,
            safe_message="模型抽取结果无法通过字段校验，需重新解析或由人工补全",
            details={"reason": type(error).__name__},
        ) from error


class EvidenceLocator(BaseModel):
    """Stable source position that maps back to the original resume.

    Mirrors the locators produced by the PDF/DOCX parser so a reviewer can jump
    from a claim straight to the exact page/paragraph/table character range.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["pdf", "docx_paragraph", "docx_table"]
    page_number: int | None = None
    block_index: int | None = None
    paragraph_index: int | None = None
    table_index: int | None = None
    row_index: int | None = None
    cell_index: int | None = None
    char_start: int
    char_end: int

    @model_validator(mode="after")
    def check_char_range(self) -> Self:
        if self.char_end < self.char_start:
            raise ValueError("char_end must not be earlier than char_start")
        return self


class EvidenceChunkCreate(BaseModel):
    """One verbatim excerpt pinned to a profile + document, from a human reviewer.

    ``candidate_profile_id`` is optional on purpose: the route binds the chunk to
    the profile *in the path* and replaces whatever the body carries, so requiring
    the body to repeat it only made every well-behaved client fail with a 422.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_profile_id: UUID | None = None
    document_id: UUID
    chunk_index: int = Field(ge=0, le=10_000)
    section_type: str = Field(min_length=1, max_length=64)
    locator: EvidenceLocator
    text: str = Field(min_length=1, max_length=20_000)

    @field_validator("section_type")
    @classmethod
    def section_type_normalized(cls, value: str) -> str:
        return value.strip().lower()


class EvidenceChunkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    candidate_profile_id: UUID
    chunk_index: int
    section_type: str
    locator_json: dict[str, Any]
    text: str
    text_sha256: str
    created_at: datetime


class CandidateProfileEdit(BaseModel):
    """Human-confirmed edits applied to a REVIEW_REQUIRED profile before READY."""

    model_config = ConfigDict(extra="forbid")

    profile_json: dict[str, Any]
    normalized_skills: list[str] = Field(default_factory=list, max_length=200)
    years_experience: float | None = Field(default=None, ge=0, le=60)
    education_level: str | None = None

    @field_validator("education_level")
    @classmethod
    def education_level_is_closed_enum(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in EDUCATION_LEVELS:
            raise ValueError(
                "education_level must be one of: " + ", ".join(EDUCATION_LEVELS)
            )
        return normalized

    @field_validator("normalized_skills")
    @classmethod
    def skills_bounded(cls, value: list[str]) -> list[str]:
        for name in value:
            if not name or len(name) > _MAX_SKILL:
                raise ValueError("each skill must be a non-empty string <= 80 chars")
        return value


class HardRuleResultResponse(BaseModel):
    """One hard-rule verdict over a candidate (mirrors ``HardRuleResult``)."""

    model_config = ConfigDict(extra="forbid")

    rule_id: Literal["years_experience", "required_education", "required_skills"]
    result: Literal["PASS", "FAIL", "UNKNOWN"]
    reason_code: str
    observed_value: object | None = None
    required_value: object | None = None


class HardRuleBundleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[HardRuleResultResponse]
    overall: Literal["PASS", "FAIL", "UNKNOWN"]


class CandidateRankingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_profile_id: UUID
    snapshot_order: int
    rrf_score: float
    display_name: str
    normalized_skills: list[str]
    years_experience: float | None
    education_level: str | None
    structured_rank: int | None = None
    structured_score: float | None = None
    keyword_rank: int | None = None
    keyword_score: float | None = None
    vector_rank: int | None = None
    vector_score: float | None = None
    hard_rule: HardRuleBundleResponse | None = None

    @classmethod
    def from_row(cls, row: CandidateRankingRow) -> CandidateRankingResponse:
        return cls(
            candidate_profile_id=row.candidate_profile_id,
            snapshot_order=row.snapshot_order,
            rrf_score=row.rrf_score,
            display_name=row.display_name,
            normalized_skills=row.normalized_skills,
            years_experience=row.years_experience,
            education_level=row.education_level,
            structured_rank=row.structured_rank,
            structured_score=row.structured_score,
            keyword_rank=row.keyword_rank,
            keyword_score=row.keyword_score,
            vector_rank=row.vector_rank,
            vector_score=row.vector_score,
            hard_rule=_hard_rule_response(row.hard_rule),
        )


def _hard_rule_response(bundle: HardRuleBundle | None) -> HardRuleBundleResponse | None:
    if bundle is None:
        return None
    return HardRuleBundleResponse(
        rules=[
            HardRuleResultResponse(
                rule_id=rule.rule_id.value,
                result=rule.result.value,
                reason_code=rule.reason_code,
                observed_value=rule.observed_value,
                required_value=rule.required_value,
            )
            for rule in bundle.rules
        ],
        overall=bundle.overall.value,
    )


class RetrievalConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top_k: int
    rrf_k: int
    structured_weight: float
    keyword_weight: float
    vector_weight: float
    rule_version: str


class CandidateListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    job_version_id: UUID
    total: int
    items: list[CandidateRankingResponse]
    config: RetrievalConfigResponse

    @classmethod
    def from_view(cls, view: CandidateRankingView, job_id: UUID) -> CandidateListResponse:
        return cls(
            job_id=job_id,
            job_version_id=view.job_version_id,
            total=view.total,
            items=[CandidateRankingResponse.from_row(row) for row in view.rows],
            config=RetrievalConfigResponse(
                top_k=view.config.top_k,
                rrf_k=view.config.rrf_k,
                structured_weight=view.config.structured_weight,
                keyword_weight=view.config.keyword_weight,
                vector_weight=view.config.vector_weight,
                rule_version=view.config.rule_version,
            ),
        )
