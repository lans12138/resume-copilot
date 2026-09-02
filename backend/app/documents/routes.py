"""Authenticated resume upload boundary."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError
from backend.app.core.settings import Settings
from backend.app.documents.models import DocumentStatus
from backend.app.documents.schemas import (
    DocumentBatchAccepted,
    DocumentListResponse,
    DocumentResponse,
)
from backend.app.documents.service import DocumentUploadService
from backend.app.infrastructure.runtime import RuntimeResources

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


def upload_service(request: Request, session: AsyncSession) -> DocumentUploadService:
    settings: Settings = request.app.state.settings
    resources: RuntimeResources = request.app.state.resources
    return DocumentUploadService(
        session,
        resources.storage,
        max_file_size_bytes=settings.max_file_size_mb * 1024 * 1024,
    )


@router.post("", response_model=DocumentBatchAccepted, status_code=202)
async def upload_documents(
    request: Request,
    files: Annotated[list[UploadFile], File()],
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> DocumentBatchAccepted:
    settings: Settings = request.app.state.settings
    if len(files) > settings.max_batch_files:
        raise AppError(
            code="BATCH_LIMIT_EXCEEDED",
            http_status=422,
            safe_message="单次上传文件数量超过限制",
            details={"maximum": settings.max_batch_files},
        )
    resources: RuntimeResources = request.app.state.resources
    try:
        async with resources.session_factory() as session:
            return await upload_service(request, session).upload_batch(actor, files)
    finally:
        for upload in files:
            await upload.close()


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: DocumentStatus | None = None,
) -> DocumentListResponse:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        return await upload_service(request, session).list_documents(
            actor, page=page, page_size=page_size, status=status
        )


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> DocumentResponse:
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        return await upload_service(request, session).get_document(actor, document_id)
