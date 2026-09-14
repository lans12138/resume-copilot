"""Authenticated resume upload boundary."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.dependencies import get_current_actor
from backend.app.auth.models import UserRole
from backend.app.auth.tokens import Actor
from backend.app.core.errors import AppError
from backend.app.core.settings import Settings
from backend.app.documents.models import DocumentStatus, ResumeDocument
from backend.app.documents.parse_service import (
    CeleryParseEnqueuer,
    DocumentParseService,
    build_parser_registry,
)
from backend.app.documents.repository import SqlAlchemyDocumentRepository
from backend.app.documents.schemas import (
    DocumentBatchAccepted,
    DocumentContentResponse,
    DocumentListResponse,
    DocumentResponse,
)
from backend.app.documents.service import DocumentUploadService, document_response
from backend.app.infrastructure.celery import app as celery_app
from backend.app.infrastructure.runtime import RuntimeResources

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


def upload_service(request: Request, session: AsyncSession) -> DocumentUploadService:
    settings: Settings = request.app.state.settings
    resources: RuntimeResources = request.app.state.resources
    return DocumentUploadService(
        session,
        resources.storage,
        max_file_size_bytes=settings.max_file_size_mb * 1024 * 1024,
        parser_version=settings.parser_version,
        enqueue=CeleryParseEnqueuer(celery_app, "documents.parse"),
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


@router.get("/{document_id}/content", response_model=DocumentContentResponse)
async def get_document_content(
    document_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> DocumentContentResponse:
    """Parsed source text and block locators, for the profile review screen.

    Document-scoped rather than job-scoped on purpose: a resume is reviewed in the
    talent pool, before it is attached to any job, so there is no job to authorize
    against yet. HR-only, read-only.
    """
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        return await upload_service(request, session).get_content(actor, document_id)


@router.post("/{document_id}/retry", response_model=DocumentResponse, status_code=202)
async def retry_document(
    document_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(get_current_actor)],
) -> DocumentResponse:
    if actor.role is not UserRole.HR:
        raise AppError(code="FORBIDDEN", http_status=403, safe_message="当前用户无简历访问权限")
    settings: Settings = request.app.state.settings
    resources: RuntimeResources = request.app.state.resources
    async with resources.session_factory() as session:
        service = DocumentParseService(
            documents=SqlAlchemyDocumentRepository(session),
            storage=resources.storage,
            parsers=build_parser_registry(parser_version=settings.parser_version),
            enqueue=CeleryParseEnqueuer(celery_app, "documents.retry_parse"),
            settings=settings,
        )
        await service.retry_parse(document_id)
        document = await session.get(ResumeDocument, document_id)
        if document is None:
            raise AppError(code="DOCUMENT_NOT_FOUND", http_status=404, safe_message="文档不存在")
        return document_response(document)
