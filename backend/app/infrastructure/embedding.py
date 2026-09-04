"""Embedding gateway abstraction (IMP-013).

``EmbeddingGateway`` is the single boundary through which the system turns
verbatim chunk text into fixed-dimension vectors. The real Qwen text-embedding
implementation lands in a later IMP; ``FakeEmbeddingGateway`` is a deterministic,
key-free stand-in used for local runs, CI, and tests. It produces a stable
``embedding_dimension``-sized vector derived from the text so retrieval tests
stay reproducible without a model backend.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Protocol

from backend.app.core.settings import Settings


class EmbeddingDimensionError(Exception):
    """Raised when a gateway returns vectors whose dimension != expected.

    This is a permanent failure: retrying with the same model cannot fix a
    dimension mismatch, so the worker must not loop on it.
    """

    def __init__(self, *, model: str, got: int, expected: int) -> None:
        super().__init__(
            f"embedding dimension mismatch for {model}: got {got}, expected {expected}"
        )
        self.model = model
        self.got = got
        self.expected = expected


class EmbeddingGateway(Protocol):
    """Async contract for turning chunk text into vectors."""

    version: str

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per input text; all vectors share the model dimension."""
        ...


def _fake_vector(text: str, dimension: int) -> list[float]:
    """Deterministic LCG-seeded vector in [0, 1) keyed by the text sha256."""
    state = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    vector: list[float] = []
    for _ in range(dimension):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        vector.append(state / 0x7FFFFFFF)
    return vector


class FakeEmbeddingGateway:
    """Heuristic, dependency-free embedder for local runs and tests."""

    def __init__(self, *, dimension: int = 1024) -> None:
        self.version = "fake-embed-v1"
        self._dimension = dimension

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [_fake_vector(text, self._dimension) for text in texts]


def build_embedding_gateway(settings: Settings) -> EmbeddingGateway:
    """Pick the embedder for the current process.

    MVP only ships the fake gateway (``mock_model_mode`` is the default). The
    real Qwen text-embedding adapter is wired in a later IMP once the API key
    and base URL contract exist.
    """
    if not settings.mock_model_mode:
        raise NotImplementedError(
            "real embedding gateway is not implemented until a later IMP; "
            "set mock_model_mode=true for local runs"
        )
    return FakeEmbeddingGateway(dimension=int(settings.embedding_dimension))
