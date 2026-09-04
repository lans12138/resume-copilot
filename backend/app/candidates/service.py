"""Profile extraction service: untrusted text -> REVIEW_REQUIRED draft.

This is the single write path that turns a parsed resume into a candidate
profile. The model output is treated as untrusted: it is re-validated at the
service boundary, and any field the model failed to produce stays null so a
human reviewer can complete it during the IMP-011 confirmation step.
"""

from __future__ import annotations

from uuid import UUID

from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.candidates.models import Candidate, CandidateProfile, CandidateProfileStatus
from backend.app.candidates.repository import (
    CandidateProfileRepository,
    CandidateRepository,
)
from backend.app.candidates.schemas import (
    CandidateProfileDraft,
    CandidateProfileResponse,
    revalidate_draft,
)
from backend.app.core.errors import AppError
from backend.app.documents.models import ResumeDocument
from backend.app.documents.parsers import ParsedDocument
from backend.app.infrastructure.model_gateway import ModelGateway, normalize_email_hash

PROFILE_SCHEMA_VERSION = "v1"
_UNKNOWN_DISPLAY_NAME = "未命名候选人"


class ProfileExtractionService:
    def __init__(
        self,
        candidate_repo: CandidateRepository,
        profile_repo: CandidateProfileRepository,
        gateway: ModelGateway,
    ) -> None:
        self._candidates = candidate_repo
        self._profiles = profile_repo
        self._gateway = gateway

    async def extract(
        self,
        *,
        actor: Actor,
        document: ResumeDocument,
        parsed: ParsedDocument,
    ) -> CandidateProfile:
        if actor.role is not UserRole.HR:
            raise AppError(
                code="FORBIDDEN",
                http_status=403,
                safe_message="当前用户无简历解析权限",
            )

        draft = await self._gateway.extract_profile(
            full_text=parsed.full_text, blocks=parsed.blocks
        )
        # Re-validate untrusted model output; raises PROFILE_DRAFT_INVALID on garbage.
        draft = revalidate_draft(draft)

        candidate = Candidate(
            display_name=draft.full_name or _UNKNOWN_DISPLAY_NAME,
            normalized_email_hash=normalize_email_hash(
                draft.contact.email if draft.contact else None
            ),
        )
        await self._candidates.save(candidate)

        version_no = await self._profiles.next_version_no(candidate.id)
        profile = CandidateProfile(
            candidate_id=candidate.id,
            document_id=document.id,
            version_no=version_no,
            status=CandidateProfileStatus.REVIEW_REQUIRED,
            profile_json=draft.model_dump(mode="json"),
            normalized_skills=_normalize_skills(draft),
            years_experience=None,
            education_level=draft.education_level,
            schema_version=PROFILE_SCHEMA_VERSION,
        )
        await self._profiles.save(profile)
        return profile

    async def get_profile(self, profile_id: UUID) -> CandidateProfileResponse:
        profile = await self._profiles.get(profile_id)
        if profile is None:
            raise AppError(
                code="PROFILE_NOT_FOUND",
                http_status=404,
                safe_message="候选人资料不存在",
            )
        return CandidateProfileResponse.model_validate(profile)


def _normalize_skills(draft: CandidateProfileDraft) -> list[str]:
    names = {claim.name.strip().casefold() for claim in draft.skills if claim.name.strip()}
    return sorted(names)
