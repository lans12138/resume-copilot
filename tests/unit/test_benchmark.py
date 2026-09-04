"""Performance baseline for the retrieval fusion step (IMP-029, §18.2).

Synthetic 100-candidate recall is fused through the pure ``fuse`` function while
the elapsed time is recorded by the in-process Histogram, establishing a p95
ceiling that later refactors must not regress.
"""

from __future__ import annotations

from uuid import uuid4

from backend.app.core.metrics import Histogram, Timer
from backend.app.retrieval.fusion import fuse
from backend.app.retrieval.models import (
    RecallBundle,
    RecallHit,
    RetrievalConfig,
)

_CANDIDATE_COUNT = 100
_ITERATIONS = 50
_P95_CEILING_SECONDS = 0.05  # generous bound for pure-python RRF over ~300 hits


def _build_bundle() -> RecallBundle:
    job_version_id = uuid4()
    ids = [uuid4() for _ in range(_CANDIDATE_COUNT)]
    structured = [
        RecallHit(profile_id=ids[i], score=1.0 - i * 0.001, rank=i + 1)
        for i in range(_CANDIDATE_COUNT)
    ]
    keyword = [
        RecallHit(profile_id=ids[(i * 3) % _CANDIDATE_COUNT], score=0.9, rank=i + 1)
        for i in range(_CANDIDATE_COUNT)
    ]
    vector = [
        RecallHit(profile_id=ids[(i * 7) % _CANDIDATE_COUNT], score=0.8, rank=i + 1)
        for i in range(_CANDIDATE_COUNT)
    ]
    return RecallBundle(
        job_version_id=job_version_id,
        structured=structured,
        keyword=keyword,
        vector=vector,
    )


def test_fusion_p95_under_baseline() -> None:
    bundle = _build_bundle()
    config = RetrievalConfig(
        structured_weight=1.0,
        keyword_weight=1.0,
        vector_weight=1.0,
        rrf_k=60,
        top_k=20,
        rule_version="benchmark-v1",
    )

    histogram = Histogram("fusion_duration_ms")
    first: list = []
    for _ in range(_ITERATIONS):
        with Timer(histogram):
            result = fuse(bundle, config)
        if not first:
            first = [candidate.candidate_profile_id for candidate in result]

    summary = histogram.snapshot()[""]
    assert summary["count"] == _ITERATIONS
    assert summary["p95"] is not None
    assert summary["p95"] < _P95_CEILING_SECONDS

    # The fused ordering is deterministic for fixed inputs.
    again = [candidate.candidate_profile_id for candidate in fuse(bundle, config)]
    assert again == first
    assert len(result) == config.top_k
