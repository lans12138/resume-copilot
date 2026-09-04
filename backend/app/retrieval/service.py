"""Recall orchestration (IMP-014).

``RetrievalService.run_recall`` fans the job version out to the three recall
channels and returns a ``RecallBundle``. It owns two side effects only: reading
projections from the repository, and embedding the job text via the gateway
(vector channel). RRF fusion of the bundle is IMP-015. The call is fully
deterministic for fixed inputs, which is the property the Golden Dataset relies
on.
"""

from __future__ import annotations

from backend.app.infrastructure.embedding import EmbeddingGateway
from backend.app.retrieval.keyword import KeywordRecaller
from backend.app.retrieval.models import (
    ChannelName,
    RecallBundle,
    RecallHit,
    RetrievalQuery,
)
from backend.app.retrieval.repository import RetrievalRepository
from backend.app.retrieval.structured import StructuredRecaller
from backend.app.retrieval.vector import VectorRecaller


class RetrievalService:
    def __init__(self, repository: RetrievalRepository, gateway: EmbeddingGateway) -> None:
        self._repository = repository
        self._gateway = gateway

    async def run_recall(self, query: RetrievalQuery) -> RecallBundle:
        job = await self._repository.get_job_query(query.job_version_id)
        profiles = await self._repository.list_ready_profiles()
        profile_ids = [profile.profile_id for profile in profiles]
        chunk_vectors = await self._repository.list_chunk_vectors(profile_ids)

        structured_hits: list[RecallHit] = []
        keyword_hits: list[RecallHit] = []
        vector_hits: list[RecallHit] = []

        if ChannelName.STRUCTURED in query.channels:
            structured_hits = StructuredRecaller().recall(job, profiles)
        if ChannelName.KEYWORD in query.channels:
            keyword_hits = KeywordRecaller().recall(job, profiles)
        if ChannelName.VECTOR in query.channels and chunk_vectors:
            job_embedding = (await self._gateway.embed([job.search_text]))[0]
            vector_hits = VectorRecaller().recall(job_embedding, chunk_vectors)

        return RecallBundle(
            job_version_id=query.job_version_id,
            structured=structured_hits,
            keyword=keyword_hits,
            vector=vector_hits,
        )
