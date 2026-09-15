"""Vector recall channel (IMP-014, §8.2).

Exact pgvector cosine search approximated in-process: the job description +
requirements are embedded into one vector, and each candidate's best-matching
evidence chunk (max cosine similarity) becomes that candidate's vector score.
Exact (no HNSW) and deterministic for a fixed gateway, so it is reproducible and
suitable for the Golden Dataset. A candidate with no embedded chunks is simply
absent from the channel.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from uuid import UUID

from backend.app.retrieval.models import ChunkVector, RecallHit


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]; 0.0 when either vector is zero-length."""
    if len(a) != len(b):
        raise ValueError(f"embedding dimension mismatch: {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass
class VectorRecaller:
    max_results: int = 1000

    def recall(
        self, job_embedding: list[float], chunk_vectors: list[ChunkVector]
    ) -> list[RecallHit]:
        best: dict[UUID, float] = {}
        for chunk in chunk_vectors:
            similarity = cosine_similarity(job_embedding, chunk.embedding)
            current = best.get(chunk.candidate_profile_id)
            if current is None or similarity > current:
                best[chunk.candidate_profile_id] = similarity
        ranked = sorted(best.items(), key=lambda item: (-item[1], item[0]))
        return [
            RecallHit(profile_id=profile_id, score=round(similarity, 6), rank=index + 1)
            for index, (profile_id, similarity) in enumerate(ranked[: self.max_results])
        ]
