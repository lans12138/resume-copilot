"""Document persistence boundary for the parse task and upload flow."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.documents.models import ResumeDocument


class DocumentRepository:
    """Async persistence boundary for resume documents.

    Production uses SQLAlchemy; tests use an in-memory implementation. The
    parse task relies only on this narrow surface so idempotency and retries
    can be exercised without PostgreSQL or Redis.
    """

    async def get_for_update(self, document_id: UUID) -> ResumeDocument | None: ...
    async def save(self, document: ResumeDocument) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...


class SqlAlchemyDocumentRepository(DocumentRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_update(self, document_id: UUID) -> ResumeDocument | None:
        stmt = (
            select(ResumeDocument)
            .where(ResumeDocument.id == document_id)
            .with_for_update()
        )
        result: ResumeDocument | None = await self._session.scalar(stmt)
        return result

    async def save(self, document: ResumeDocument) -> None:
        self._session.add(document)

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    async def close(self) -> None:
        await self._session.close()


def session_repository(
    session_factory: async_sessionmaker[AsyncSession],
) -> SqlAlchemyDocumentRepository:
    return SqlAlchemyDocumentRepository(session_factory())
