"""Profile extraction service: untrusted text -> REVIEW_REQUIRED draft.

This is the single write path that turns a parsed resume into a candidate
profile. The model output is treated as untrusted: it is re-validated at the
service boundary, and any field the model failed to produce stays null so a
human reviewer can complete it during the IMP-011 confirmation step.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.candidates.embedding_service import EmbeddingEnqueuer
from backend.app.candidates.models import (
    Candidate,
    CandidateProfile,
    CandidateProfileStatus,
    EvidenceChunk,
)
from backend.app.candidates.repository import (
    CandidateProfileRepository,
    CandidateRepository,
    EvidenceChunkRepository,
)
from backend.app.candidates.schemas import (
    CandidateProfileDraft,
    CandidateProfileEdit,
    CandidateProfileResponse,
    EvidenceChunkCreate,
    EvidenceChunkResponse,
    revalidate_draft,
)
from backend.app.core.errors import AppError, app_error
from backend.app.documents.models import DocumentStatus, ResumeDocument
from backend.app.documents.parsers import ParsedDocument
from backend.app.documents.repository import DocumentRepository
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


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _assert_hr(actor: Actor) -> None:
    if actor.role is not UserRole.HR:
        raise app_error(
            code="FORBIDDEN",
            http_status=403,
            safe_message="当前用户无简历校对权限",
        )


class ProfileReviewService:
    """Human confirmation and evidence pinning for extracted profiles.

    Turns a REVIEW_REQUIRED draft into a READY, versioned, and human-attested
    profile, and lets reviewers pin verbatim evidence chunks. Both write paths
    are HR-only and enforce the D09 invariants: optimistic-lock conflict, single
    READY version per candidate, and rejection of cross-document evidence.
    """

    def __init__(
        self,
        profile_repo: CandidateProfileRepository,
        chunk_repo: EvidenceChunkRepository,
        documents: DocumentRepository | None = None,
    ) -> None:
        self._profiles = profile_repo
        self._chunks = chunk_repo
        self._documents = documents

    async def confirm_profile(
        self,
        *,
        actor: Actor,
        profile_id: UUID,
        edit: CandidateProfileEdit,
        expected_version: int,
        enqueue: EmbeddingEnqueuer | None = None,
    ) -> CandidateProfileResponse:
        _assert_hr(actor)

        profile = await self._profiles.get(profile_id)
        if profile is None:
            raise app_error(
                code="PROFILE_NOT_FOUND",
                http_status=404,
                safe_message="候选人资料不存在",
            )
        if profile.status is not CandidateProfileStatus.REVIEW_REQUIRED:
            raise app_error(
                code="PROFILE_NOT_REVIEWABLE",
                http_status=409,
                safe_message="只有待校对的资料可以确认",
                details={"status": profile.status.value},
            )
        # Optimistic-lock guard: a stale edit must not clobber a newer confirmation.
        if profile.version != expected_version:
            raise app_error(
                code="PROFILE_VERSION_CONFLICT",
                http_status=409,
                safe_message="资料已被其他人修改，请刷新后重试",
                details={"expected": expected_version, "current": profile.version},
                retryable=True,
            )

        # §7.4: confirmation also closes the parse lifecycle. The document that
        # produced this profile must leave REVIEW_REQUIRED in the same
        # transaction, so a settled resume can never be re-extracted behind a
        # confirmed profile.
        document: ResumeDocument | None = None
        if self._documents is not None:
            document = await self._documents.get_for_update(profile.document_id)
            if document is None:
                raise app_error(
                    code="DOCUMENT_NOT_FOUND",
                    http_status=404,
                    safe_message="简历文档不存在",
                )
            if document.status is not DocumentStatus.REVIEW_REQUIRED:
                raise app_error(
                    code="DOCUMENT_NOT_REVIEWABLE",
                    http_status=409,
                    safe_message="简历文档当前状态不可确认",
                    details={"status": document.status.value},
                )

        profile.profile_json = edit.profile_json
        profile.normalized_skills = sorted(
            {s.strip().casefold() for s in edit.normalized_skills if s.strip()}
        )
        profile.years_experience = edit.years_experience
        profile.education_level = edit.education_level

        # Exactly one READY profile per candidate: supersede any prior READY version.
        for prior in await self._profiles.list_ready_versions(profile.candidate_id):
            if prior.id != profile.id:
                prior.status = CandidateProfileStatus.SUPERSEDED

        profile.status = CandidateProfileStatus.READY
        profile.confirmed_by = actor.user_id
        profile.confirmed_at = datetime.now(UTC)
        profile.version += 1
        await self._profiles.save(profile)
        if document is not None and self._documents is not None:
            document.status = DocumentStatus.READY
            await self._documents.save(document)
        # The response is assembled from the very instance this call just mutated,
        # so the UPDATE has to be flushed (and the server-generated ``updated_at``
        # re-read) *before* pydantic touches the object: a read of an expired
        # attribute inside an async session raises MissingGreenlet, not a value.
        # Flushing first also means the embedding task below can never be published
        # ahead of the rows it points at.
        await self._profiles.flush_and_refresh(profile)
        # §7.2: the embedding task for the profile's evidence chunks is published
        # once the confirmed rows exist in the transaction. Chunks were persisted in
        # an earlier request, so a worker picking this up sees no gap.
        if enqueue is not None:
            enqueue.enqueue(profile_id=profile_id)
        return CandidateProfileResponse.model_validate(profile)

    async def create_evidence_chunks(
        self,
        *,
        actor: Actor,
        chunks: list[EvidenceChunkCreate],
    ) -> list[EvidenceChunkResponse]:
        _assert_hr(actor)

        created: list[EvidenceChunk] = []
        seen_index: set[tuple[UUID, int]] = set()
        for item in chunks:
            # The route binds each chunk to the profile in the path before calling
            # this; an unbound chunk means the caller skipped that step, and silently
            # guessing a profile is exactly the confusion this guard prevents.
            if item.candidate_profile_id is None:
                raise app_error(
                    code="EVIDENCE_PROFILE_REQUIRED",
                    http_status=422,
                    safe_message="证据必须绑定到候选人资料",
                )
            profile = await self._profiles.get(item.candidate_profile_id)
            if profile is None:
                raise app_error(
                    code="PROFILE_NOT_FOUND",
                    http_status=404,
                    safe_message="证据引用的候选人资料不存在",
                )
            # Cross-document evidence is rejected: a chunk must reference the exact
            # document the profile was extracted from (also enforced by the composite FK).
            if item.document_id != profile.document_id:
                raise app_error(
                    code="EVIDENCE_CROSS_DOCUMENT_REJECTED",
                    http_status=422,
                    safe_message="证据引用的文档不属于该候选人资料",
                    details={"profile_document_id": str(profile.document_id)},
                )
            duplicate_in_batch = (item.document_id, item.chunk_index) in seen_index
            already_stored = await self._chunks.exists_index(item.document_id, item.chunk_index)
            if duplicate_in_batch or already_stored:
                raise app_error(
                    code="EVIDENCE_CHUNK_INDEX_DUPLICATE",
                    http_status=409,
                    safe_message="同一文档的相同证据序号已存在",
                    details={"chunk_index": item.chunk_index},
                )
            seen_index.add((item.document_id, item.chunk_index))

            chunk = EvidenceChunk(
                id=uuid4(),
                document_id=item.document_id,
                candidate_profile_id=item.candidate_profile_id,
                chunk_index=item.chunk_index,
                section_type=item.section_type,
                locator_json=item.locator.model_dump(),
                text=item.text,
                text_sha256=_sha256(item.text),
                created_at=datetime.now(UTC),
            )
            await self._chunks.save(chunk)
            created.append(chunk)
        return [EvidenceChunkResponse.model_validate(c) for c in created]

    async def list_evidence(
        self,
        *,
        actor: Actor,
        profile_id: UUID,
    ) -> list[EvidenceChunkResponse]:
        profile = await self._profiles.get(profile_id)
        if profile is None:
            raise app_error(
                code="PROFILE_NOT_FOUND",
                http_status=404,
                safe_message="候选人资料不存在",
            )
        chunks = await self._chunks.list_by_profile(profile_id)
        return [EvidenceChunkResponse.model_validate(c) for c in chunks]
