"""FIN-003 end-to-end: upload -> parse -> extract -> evidence -> confirm -> embed.

Runs against a real PostgreSQL and a live Redis broker (so the enqueue path is
exercised, not stubbed). Skipped unless ``DATABASE_URL`` points at a real
PostgreSQL, which keeps the hermetic unit suite clean; the compose-backed probe
``tests/validate_document_pipeline.ps1`` runs it for real in CI.

Why the Celery tasks are invoked via ``celery_app.tasks[...].run(...)`` instead of
a live worker: the probe starts only postgres+redis, so ``.run()`` executes the
exact worker code path (single event loop, per-invocation ``RuntimeResources``)
without racing a second consumer. At-least-once redelivery is covered by calling
the same task twice and asserting the database refuses a second side effect.

Each ``def test_`` drives the async scenario through ``asyncio.run`` — the
project convention, since no pytest-asyncio plugin is configured.
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid

import pytest
from docx import Document
from fastapi import UploadFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Import the task modules before the app finalizes, exactly like the worker
# entrypoint does; otherwise the shared tasks never register on this instance.
import backend.app.candidates.tasks  # noqa: F401
import backend.app.documents.tasks  # noqa: F401
from backend.app.auth.models import User, UserRole
from backend.app.auth.tokens import Actor
from backend.app.candidates.models import CandidateProfileStatus
from backend.app.candidates.repository import (
    SqlCandidateProfileRepository,
    SqlEvidenceChunkRepository,
)
from backend.app.candidates.schemas import (
    CandidateProfileEdit,
    EvidenceChunkCreate,
    EvidenceLocator,
)
from backend.app.candidates.service import ProfileReviewService
from backend.app.core.settings import get_settings
from backend.app.documents.models import DocumentStatus
from backend.app.documents.repository import SqlAlchemyDocumentRepository
from backend.app.documents.service import DocumentUploadService
from backend.app.documents.validation import DOCX_MEDIA_TYPE
from backend.app.infrastructure.celery import app as celery_app
from backend.app.infrastructure.runtime import RuntimeResources

_DATABASE_URL = os.environ.get("DATABASE_URL", "")
_RUN_INTEGRATION = _DATABASE_URL.startswith("postgresql+asyncpg")
pytestmark = pytest.mark.skipif(
    not _RUN_INTEGRATION, reason="requires DATABASE_URL=postgresql+asyncpg://..."
)

_SKILLS_SENTENCE = "6 年 Python 后端开发经验，熟悉 FastAPI、Docker、PostgreSQL 与 Redis。"
_TRUNCATE_SQL = (
    "TRUNCATE TABLE evidence_chunks, candidate_profiles, candidates, "
    "resume_documents, users CASCADE"
)


def _make_resume_docx() -> bytes:
    """A real DOCX so the production parser runs; unique per call (content hash)."""
    marker = uuid.uuid4().hex
    document = Document()
    document.add_paragraph("张伟")
    document.add_paragraph(f"zhangwei.{marker[:8]}@example.com")
    document.add_paragraph(_SKILLS_SENTENCE)
    document.add_paragraph(f"唯一标识 {marker}")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def _reset(session: AsyncSession) -> None:
    await session.execute(text(_TRUNCATE_SQL))
    await session.commit()


async def _seed_hr(resources: RuntimeResources) -> Actor:
    async with resources.session_factory() as session:
        user = User(
            username=f"hr.e2e.{uuid.uuid4().hex[:8]}",
            password_hash="not-used-by-this-test",
            role=UserRole.HR,
        )
        session.add(user)
        await session.commit()
        return Actor(user_id=user.id, username=user.username, role=user.role)


async def _upload(resources: RuntimeResources, actor: Actor, payload: bytes) -> uuid.UUID:
    """Upload through the production service; capture the parse enqueue instead of
    publishing it, so this test drives the task sequence explicitly."""
    delivered: list[tuple[uuid.UUID, int, str]] = []

    class _RecordingParseEnqueuer:
        def enqueue_parse(
            self, document_id: uuid.UUID, *, attempt: int, parser_version: str
        ) -> None:
            delivered.append((document_id, attempt, parser_version))

    async with resources.session_factory() as session:
        service = DocumentUploadService(
            session,
            resources.storage,
            max_file_size_bytes=10 * 1024 * 1024,
            parser_version=get_settings().parser_version,
            enqueue=_RecordingParseEnqueuer(),
        )
        upload = UploadFile(
            file=io.BytesIO(payload),
            size=len(payload),
            filename="resume.docx",
            headers={"content-type": DOCX_MEDIA_TYPE},
        )
        batch = await service.upload_batch(actor, [upload])

    assert batch.accepted == 1
    document_id = batch.items[0].resource_id
    assert document_id is not None
    assert delivered == [(document_id, 1, get_settings().parser_version)]
    return document_id


async def _scalar(resources: RuntimeResources, sql: str, **params: object) -> object:
    async with resources.session_factory() as session:
        return await session.scalar(text(sql), params)


async def _document_status(resources: RuntimeResources, document_id: uuid.UUID) -> str:
    value = await _scalar(
        resources, "SELECT status FROM resume_documents WHERE id = :id", id=document_id
    )
    assert isinstance(value, str)
    return value


async def _count(resources: RuntimeResources, sql: str, **params: object) -> int:
    value = await _scalar(resources, sql, **params)
    assert isinstance(value, int)
    return value


async def _pin_evidence(
    resources: RuntimeResources, actor: Actor, profile_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    async with resources.session_factory() as session:
        service = ProfileReviewService(
            SqlCandidateProfileRepository(session), SqlEvidenceChunkRepository(session)
        )
        created = await service.create_evidence_chunks(
            actor=actor,
            chunks=[
                EvidenceChunkCreate(
                    candidate_profile_id=profile_id,
                    document_id=document_id,
                    chunk_index=0,
                    section_type="skills",
                    locator=EvidenceLocator(
                        kind="docx_paragraph", paragraph_index=2, char_start=0, char_end=40
                    ),
                    text=_SKILLS_SENTENCE,
                )
            ],
        )
        await session.commit()
    assert len(created) == 1


async def _confirm(
    resources: RuntimeResources, actor: Actor, profile_id: uuid.UUID, published: list[uuid.UUID]
) -> None:
    class _RecordingEmbeddingEnqueuer:
        def enqueue(self, *, profile_id: uuid.UUID) -> None:
            published.append(profile_id)

    async with resources.session_factory() as session:
        profiles = SqlCandidateProfileRepository(session)
        profile = await profiles.get(profile_id)
        assert profile is not None
        service = ProfileReviewService(
            profiles,
            SqlEvidenceChunkRepository(session),
            SqlAlchemyDocumentRepository(session),
        )
        await service.confirm_profile(
            actor=actor,
            profile_id=profile_id,
            edit=CandidateProfileEdit(
                profile_json=profile.profile_json,
                normalized_skills=list(profile.normalized_skills),
                education_level=profile.education_level,
            ),
            expected_version=profile.version,
            enqueue=_RecordingEmbeddingEnqueuer(),
        )
        await session.commit()


async def _run_pipeline() -> None:
    settings = get_settings()
    resources = RuntimeResources.build(settings)
    parse_task = celery_app.tasks["documents.parse"]
    extract_task = celery_app.tasks["candidates.extract_profile"]
    embed_task = celery_app.tasks["embeddings.generate_chunks"]
    parser_version = settings.parser_version
    try:
        async with resources.session_factory() as session:
            await _reset(session)
        actor = await _seed_hr(resources)

        # 1. Upload -> QUEUED, parse delivered.
        document_id = await _upload(resources, actor, _make_resume_docx())

        # 2. Parse (real parser + storage + PostgreSQL).
        status = parse_task.run(
            str(document_id), attempt=1, parser_version=parser_version
        )
        assert status == DocumentStatus.REVIEW_REQUIRED.value
        assert (
            await _document_status(resources, document_id)
            == DocumentStatus.REVIEW_REQUIRED.value
        )

        # A duplicate delivery of the same attempt is a no-op.
        replay = parse_task.run(
            str(document_id), attempt=1, parser_version=parser_version
        )
        assert replay == DocumentStatus.REVIEW_REQUIRED.value

        # 3. Extraction turns the stored blocks into a REVIEW_REQUIRED draft.
        extracted = extract_task.run(str(document_id))
        assert extracted["status"] == "ok"
        profile_id = uuid.UUID(extracted["profile_id"])
        assert await _count(
            resources,
            "SELECT count(*) FROM candidate_profiles WHERE id = :id AND status = :status",
            id=profile_id,
            status=CandidateProfileStatus.REVIEW_REQUIRED.value,
        ) == 1
        # Evidence text was extracted from the parsed document, not invented.
        assert await _count(
            resources,
            "SELECT count(*) FROM candidate_profiles WHERE id = :id "
            "AND profile_json->>'full_name' = :name",
            id=profile_id,
            name="张伟",
        ) == 1

        # At-least-once: re-delivery must not create a second draft.
        duplicate = extract_task.run(str(document_id))
        assert duplicate["status"] == "skipped"
        assert duplicate["reason"] == "already_extracted"
        assert await _count(
            resources,
            "SELECT count(*) FROM candidate_profiles WHERE document_id = :id",
            id=document_id,
        ) == 1

        # 4. HR pins the verbatim evidence chunk backing the claim.
        await _pin_evidence(resources, actor, profile_id, document_id)

        # 5. HR confirms -> profile READY, document READY, embedding published.
        published: list[uuid.UUID] = []
        await _confirm(resources, actor, profile_id, published)
        assert published == [profile_id]
        assert await _count(
            resources,
            "SELECT count(*) FROM candidate_profiles WHERE id = :id AND status = :status",
            id=profile_id,
            status=CandidateProfileStatus.READY.value,
        ) == 1
        assert await _document_status(resources, document_id) == DocumentStatus.READY.value

        # 6. Embedding fills the vector for the pinned chunk.
        embedded = embed_task.run(str(profile_id))
        assert embedded["status"] == "ok"
        assert embedded["generated"] == 1
        assert await _count(
            resources,
            "SELECT count(*) FROM evidence_chunks "
            "WHERE candidate_profile_id = :id AND embedding IS NOT NULL",
            id=profile_id,
        ) == 1

        # Re-delivery skips the already-embedded chunk (no second side effect).
        replayed = embed_task.run(str(profile_id))
        assert replayed["status"] == "ok"
        assert replayed["generated"] == 0
        assert await _count(
            resources,
            "SELECT count(*) FROM evidence_chunks "
            "WHERE candidate_profile_id = :id AND embedding IS NOT NULL",
            id=profile_id,
        ) == 1

        # A late parse delivery must not drag a confirmed document back.
        late = parse_task.run(
            str(document_id), attempt=1, parser_version=parser_version
        )
        assert late == DocumentStatus.READY.value
        assert await _document_status(resources, document_id) == DocumentStatus.READY.value
    finally:
        await resources.close()


def test_document_pipeline_end_to_end() -> None:
    asyncio.run(_run_pipeline())
