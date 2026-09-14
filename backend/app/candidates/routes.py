"""Candidate profile & evidence API (FIN-003).

Adds the HR-facing closed loop on top of the existing read-only ranking preview
(IMP-017): read a candidate's extracted Profile, let an HR reviewer confirm/edit
it (publishing the embedding task on success), and list/pin the verbatim
EvidenceChunks that back each claim. Every write path is HR-only and uses the
job-level resource authorization (``JobService.get_authorized``) plus the service
layer's optimistic-lock / single-READY / cross-document guards.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.candidates.embedding_service import CeleryEmbeddingEnqueuer
from backend.app.candidates.repository import (
    SqlCandidateProfileRepository,
    SqlEvidenceChunkRepository,
)
from backend.app.candidates.schemas import (
    CandidateListResponse,
    CandidateProfileEdit,
    CandidateProfileResponse,
    EvidenceChunkCreate,
    EvidenceChunkResponse,
)
from backend.app.candidates.service import ProfileReviewService
from backend.app.core.errors import AppError
from backend.app.infrastructure.celery import app as celery_app
from backend.app.infrastructure.embedding import build_embedding_gateway
from backend.app.infrastructure.runtime import RuntimeResources
from backend.app.jobs.service import JobService
from backend.app.retrieval.models import ChannelName, HardRuleOutcome, RetrievalConfig
from backend.app.retrieval.preview import CandidateFilter, preview_ranking
from backend.app.retrieval.repository import SqlRetrievalRepository

router = APIRouter(prefix="/api/v1/jobs", tags=["candidates"])


@router.get("/{job_id}/candidates", response_model=CandidateListResponse)
async def list_job_candidates(
    job_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
    hard_rule: Annotated[HardRuleOutcome | None, Query()] = None,
    channel: Annotated[ChannelName | None, Query()] = None,
) -> CandidateListResponse:
    resources: RuntimeResources = request.app.state.resources
    settings = request.app.state.settings
    async with resources.session_factory() as session:
        job_service = JobService(session)
        job = await job_service.get_authorized(actor, job_id)
        version = await job_service.current_version(job)
        retrieval_repo = SqlRetrievalRepository(session)
        profile_repo = SqlCandidateProfileRepository(session)
        views = {view.profile_id: view for view in await profile_repo.list_ready_views()}
        gateway = build_embedding_gateway(settings)
        config = RetrievalConfig.from_settings(settings)
        preview = await preview_ranking(
            retrieval_repo,
            gateway,
            version.id,
            config,
            CandidateFilter(hard_rule=hard_rule, channel=channel),
            views,
        )
        return CandidateListResponse.from_view(preview, job_id)


@router.get("/{job_id}/profiles/{profile_id}", response_model=CandidateProfileResponse)
async def get_candidate_profile(
    job_id: UUID,
    profile_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> CandidateProfileResponse:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        job_service = JobService(session)
        await job_service.get_authorized(actor, job_id)
        profile_repo = SqlCandidateProfileRepository(session)
        profile = await profile_repo.get(profile_id)
        if profile is None:
            raise AppError(
                code="PROFILE_NOT_FOUND",
                http_status=404,
                safe_message="候选人资料不存在",
            )
        return CandidateProfileResponse.model_validate(profile)


@router.post("/{job_id}/profiles/{profile_id}/confirm", response_model=CandidateProfileResponse)
async def confirm_candidate_profile(
    job_id: UUID,
    profile_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
    edit: CandidateProfileEdit,
    expected_version: Annotated[int, Query()],
) -> CandidateProfileResponse:
    """HR confirms an edited REVIEW_REQUIRED profile.

    ``expected_version`` is the optimistic-lock token the client last saw; a
    mismatch aborts with 409 (PROFILE_VERSION_CONFLICT). On success the profile
    reaches READY and the embedding task for its evidence chunks is enqueued.
    """
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        job_service = JobService(session)
        await job_service.get_authorized(actor, job_id)
        profile_repo = SqlCandidateProfileRepository(session)
        chunk_repo = SqlEvidenceChunkRepository(session)
        service = ProfileReviewService(profile_repo, chunk_repo)
        result = await service.confirm_profile(
            actor=actor,
            profile_id=profile_id,
            edit=edit,
            expected_version=expected_version,
            enqueue=CeleryEmbeddingEnqueuer(celery_app, "embeddings.generate_chunks"),
        )
        await session.commit()
        return result


@router.get(
    "/{job_id}/profiles/{profile_id}/evidence",
    response_model=list[EvidenceChunkResponse],
)
async def list_candidate_evidence(
    job_id: UUID,
    profile_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> list[EvidenceChunkResponse]:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        job_service = JobService(session)
        await job_service.get_authorized(actor, job_id)
        profile_repo = SqlCandidateProfileRepository(session)
        chunk_repo = SqlEvidenceChunkRepository(session)
        service = ProfileReviewService(profile_repo, chunk_repo)
        return await service.list_evidence(actor=actor, profile_id=profile_id)


@router.post(
    "/{job_id}/profiles/{profile_id}/evidence",
    response_model=list[EvidenceChunkResponse],
    status_code=201,
)
async def create_candidate_evidence(
    job_id: UUID,
    profile_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
    chunks: list[EvidenceChunkCreate],
) -> list[EvidenceChunkResponse]:
    """HR pins verbatim evidence chunks onto a profile (bounded to this profile)."""
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        job_service = JobService(session)
        await job_service.get_authorized(actor, job_id)
        profile_repo = SqlCandidateProfileRepository(session)
        chunk_repo = SqlEvidenceChunkRepository(session)
        service = ProfileReviewService(profile_repo, chunk_repo)
        # The URL owns the profile binding; ignore any client-supplied id so a
        # chunk can never be pinned to a different profile than the path says.
        bound = [_bind(chunk, profile_id) for chunk in chunks]
        result = await service.create_evidence_chunks(actor=actor, chunks=bound)
        await session.commit()
        return result


def _bind(chunk: EvidenceChunkCreate, profile_id: UUID) -> EvidenceChunkCreate:
    return EvidenceChunkCreate(
        candidate_profile_id=profile_id,
        document_id=chunk.document_id,
        chunk_index=chunk.chunk_index,
        section_type=chunk.section_type,
        locator=chunk.locator,
        text=chunk.text,
    )
