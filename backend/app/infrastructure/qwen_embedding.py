"""OpenAI-compatible Qwen embedding gateway (FIN-008).

Implements the ``EmbeddingGateway`` protocol against the OpenAI-compatible
``/embeddings`` shape (§12.3, 1024-dim ``qwen3.7-text-embedding``).

The one thing this adapter must get right is **ordering**. The upstream returns
embeddings each tagged with an ``index``, and nothing guarantees the array is
sorted. The contract of ``EmbeddingGateway.embed`` is positional — vector *i*
belongs to text *i* — so the response is re-sorted by index and then checked for
length, gaps and duplicates before any vector is handed back. Silently trusting
the arrival order would attach the wrong vector to the wrong chunk, which does
not fail loudly; it just makes retrieval quietly wrong, and it would be
attributed to the retriever rather than to this adapter.

A dimension mismatch raises ``EmbeddingDimensionError``, which the existing
Celery layer already treats as permanent (retrying cannot change a model's
output width).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from backend.app.core.settings import Settings
from backend.app.infrastructure.embedding import EmbeddingDimensionError
from backend.app.infrastructure.http_transport import (
    JsonTransport,
    ResponseSchemaError,
    parse_json_object,
)

logger = logging.getLogger(__name__)

EMBEDDINGS_PATH = "/embeddings"


class EmbeddingResponseShapeError(ResponseSchemaError):
    """The provider returned 200 but the embedding envelope was unusable."""


def _ordered_vectors(body: dict[str, Any], expected_count: int) -> list[list[float]]:
    """Return one vector per input, ordered by the provider's own index.

    Raises a permanent shape error when the payload is malformed. Every check
    here exists because getting it wrong produces *silently* misaligned vectors
    rather than an exception.
    """
    data = body.get("data")
    if not isinstance(data, list):
        raise EmbeddingResponseShapeError("embedding response carried no data array")
    if len(data) != expected_count:
        raise EmbeddingResponseShapeError(
            "embedding response length did not match the request",
            details={"expected": expected_count, "got": len(data)},
        )

    indexed: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise EmbeddingResponseShapeError("embedding entry was not an object")
        index = item.get("index")
        vector = item.get("embedding")
        if not isinstance(index, int) or isinstance(index, bool):
            raise EmbeddingResponseShapeError("embedding entry carried no integer index")
        if not isinstance(vector, list) or not vector:
            raise EmbeddingResponseShapeError("embedding entry carried no vector")
        if index in indexed:
            raise EmbeddingResponseShapeError("embedding response repeated an index")
        try:
            indexed[index] = [float(value) for value in vector]
        except (TypeError, ValueError) as error:
            raise EmbeddingResponseShapeError("embedding vector held non-numeric values") from error

    # A gap or an out-of-range index means we cannot map every input to a vector,
    # so there is no safe partial result to return.
    if set(indexed) != set(range(expected_count)):
        raise EmbeddingResponseShapeError(
            "embedding response indices did not cover the request",
            details={"expected": expected_count, "got": sorted(indexed)},
        )
    return [indexed[position] for position in range(expected_count)]


class QwenEmbeddingGateway:
    """Real embedder against an OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimension: int,
        transport: JsonTransport,
        timeout_seconds: float,
    ) -> None:
        self.version = f"qwen-embed:{model}"
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._dimension = dimension
        self._transport = transport
        self._timeout = timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dimension

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._api_key}"}

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per text, positionally aligned with the input."""
        if not texts:
            # No request at all: an empty batch has an empty answer, and sending
            # it would burn a round trip and could be rejected as invalid.
            return []

        payload: dict[str, Any] = {
            "model": self._model,
            "input": list(texts),
            "encoding_format": "float",
        }
        body = await self._transport.post_json(
            f"{self._base_url}{EMBEDDINGS_PATH}",
            payload,
            headers=self._headers(),
            timeout=self._timeout,
        )
        envelope = parse_json_object(body)
        vectors = _ordered_vectors(dict(envelope), len(texts))

        for vector in vectors:
            if len(vector) != self._dimension:
                # Permanent: the model's output width is a property of the model,
                # so a retry cannot fix it and the worker must not loop.
                raise EmbeddingDimensionError(
                    model=self._model, got=len(vector), expected=self._dimension
                )

        logger.info(
            "qwen_embedding_completed",
            extra={"model": self._model, "batch_size": len(texts), "dimension": self._dimension},
        )
        return vectors


def build_qwen_embedding_gateway(
    settings: Settings, *, transport: JsonTransport | None = None
) -> QwenEmbeddingGateway:
    """Build the real embedder from settings for the current process."""
    # Imported here so the module stays importable without httpx2 at type-check
    # time, and to avoid a circular import through the transport factory.
    sender: JsonTransport
    if transport is not None:
        sender = transport
    else:
        from backend.app.infrastructure.http_transport import HttpxJsonSender

        sender = JsonTransport(HttpxJsonSender(), max_attempts=3)
    return QwenEmbeddingGateway(
        base_url=settings.model_base_url or "",
        api_key=(
            settings.qwen_api_key.get_secret_value() if settings.qwen_api_key is not None else ""
        ),
        model=settings.embedding_model,
        dimension=int(settings.embedding_dimension),
        transport=sender,
        timeout_seconds=float(settings.model_timeout_seconds),
    )


__all__ = [
    "EMBEDDINGS_PATH",
    "EmbeddingResponseShapeError",
    "QwenEmbeddingGateway",
    "build_qwen_embedding_gateway",
]
