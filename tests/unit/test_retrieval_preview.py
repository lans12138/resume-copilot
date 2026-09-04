"""IMP-017 contract tests for the candidate ranking preview (D15 regression).

Uses the hermetic ``InMemoryRetrievalRepository`` + ``FakeEmbeddingGateway`` so
the test never touches Postgres or a model backend. The key invariant is that
the explicit UI filter only narrows what is *shown*: ``total`` always reports
the full Top-K size, so filtering can never shrink the MatchRun candidate set.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from backend.app.infrastructure.embedding import FakeEmbeddingGateway
from backend.app.retrieval.models import (
    ChannelName,
    ChunkVector,
    HardRuleOutcome,
    JobQuery,
    ReadyProfile,
    ReadyProfileView,
    RetrievalConfig,
)
from backend.app.retrieval.preview import (
    CandidateFilter,
    CandidateRankingView,
    preview_ranking,
)
from backend.app.retrieval.repository import InMemoryRetrievalRepository

JOB_VERSION_ID = UUID("99999999-9999-4999-8999-999999999999")
A_ID = UUID("11111111-1111-4111-8111-111111111111")
B_ID = UUID("22222222-2222-4222-8222-222222222222")
C_ID = UUID("33333333-3333-4333-8333-333333333333")


def _config() -> RetrievalConfig:
    return RetrievalConfig(
        structured_weight=1.0,
        keyword_weight=1.0,
        vector_weight=1.0,
        rrf_k=60,
        top_k=10,
        rule_version="v1",
    )


def _job() -> JobQuery:
    return JobQuery(
        job_version_id=JOB_VERSION_ID,
        required_skills=["python"],
        preferred_skills=[],
        min_years=3.0,
        required_education="本科",
        description_text="backend engineer",
        requirements_json={},
    )


def _profiles() -> list[ReadyProfile]:
    return [
        ReadyProfile(A_ID, ["python"], 5.0, "硕士", {}),
        ReadyProfile(B_ID, ["java"], 1.0, "大专", {}),
        ReadyProfile(C_ID, ["python"], None, "硕士", {}),
    ]


def _views() -> dict[UUID, ReadyProfileView]:
    return {
        A_ID: ReadyProfileView(A_ID, "Alice", ["python"], 5.0, "硕士"),
        B_ID: ReadyProfileView(B_ID, "Bob", ["java"], 1.0, "大专"),
        C_ID: ReadyProfileView(C_ID, "Carol", ["python"], None, "硕士"),
    }


def _repository() -> InMemoryRetrievalRepository:
    gateway = FakeEmbeddingGateway(dimension=1024)
    job_embedding = asyncio.run(gateway.embed([_job().search_text]))[0]
    return InMemoryRetrievalRepository(
        profiles=_profiles(),
        chunk_vectors=[ChunkVector(candidate_profile_id=A_ID, embedding=job_embedding)],
        job_queries={JOB_VERSION_ID: _job()},
    )


def _preview(
    filters: CandidateFilter | None = None,
    *,
    channels: tuple[ChannelName, ...] | None = None,
) -> CandidateRankingView:
    config = _config()
    if channels is not None:
        config = RetrievalConfig(
            structured_weight=config.structured_weight,
            keyword_weight=config.keyword_weight,
            vector_weight=config.vector_weight,
            rrf_k=config.rrf_k,
            top_k=config.top_k,
            rule_version=config.rule_version,
            channels=channels,
        )
    return asyncio.run(
        preview_ranking(
            _repository(),
            FakeEmbeddingGateway(dimension=1024),
            JOB_VERSION_ID,
            config,
            filters or CandidateFilter(),
            _views(),
        )
    )


def test_ranking_order_and_total() -> None:
    view = _preview()
    assert view.total == 3  # noqa: SLF001
    assert len(view.rows) == 3  # noqa: SLF001
    orders = [row.snapshot_order for row in view.rows]  # noqa: SLF001
    assert orders == [1, 2, 3]
    scores = [row.rrf_score for row in view.rows]  # noqa: SLF001
    assert scores == sorted(scores, reverse=True)


def test_channel_results_propagate() -> None:
    view = _preview()
    by_id = {row.candidate_profile_id: row for row in view.rows}  # noqa: SLF001
    # A has a vector chunk, so all three channels rank it.
    assert by_id[A_ID].structured_rank is not None
    assert by_id[A_ID].keyword_rank is not None
    assert by_id[A_ID].vector_rank == 1
    # B and C have no chunk, so the vector channel never ranks them.
    assert by_id[B_ID].vector_rank is None
    assert by_id[C_ID].vector_rank is None
    assert by_id[B_ID].structured_rank is not None
    assert by_id[C_ID].keyword_rank is not None


def test_hard_rule_verdicts() -> None:
    view = _preview()
    rows = {row.candidate_profile_id: row for row in view.rows}  # noqa: SLF001
    a, b, c = rows[A_ID], rows[B_ID], rows[C_ID]
    assert a.hard_rule is not None and a.hard_rule.overall == HardRuleOutcome.PASS
    assert b.hard_rule is not None and b.hard_rule.overall == HardRuleOutcome.FAIL
    # C is missing years (job requires 3) while skills/education pass -> UNKNOWN.
    assert c.hard_rule is not None and c.hard_rule.overall == HardRuleOutcome.UNKNOWN


def test_explicit_filter_does_not_shrink_match_run_scope() -> None:
    full = _preview()
    has_fail = any(
        row.hard_rule is not None and row.hard_rule.overall == HardRuleOutcome.FAIL
        for row in full.rows
    )
    assert has_fail

    pass_only = _preview(CandidateFilter(hard_rule=HardRuleOutcome.PASS))
    # The filter narrows what is shown ...
    assert len(pass_only.rows) == 1  # noqa: SLF001
    assert pass_only.rows[0].candidate_profile_id == A_ID  # noqa: SLF001
    # ... but `total` still reports the full Top-K, so the MatchRun scope is intact.
    assert pass_only.total == full.total == 3  # noqa: SLF001


def test_channel_filter_only_keeps_hits() -> None:
    view = _preview(CandidateFilter(channel=ChannelName.VECTOR))
    assert len(view.rows) == 1  # noqa: SLF001
    assert view.rows[0].candidate_profile_id == A_ID  # noqa: SLF001
    assert view.total == 3  # noqa: SLF001


def test_single_channel_isolation() -> None:
    view = _preview(channels=(ChannelName.VECTOR,))
    # Only A has a vector chunk, so it is the sole candidate under vector-only recall.
    assert view.total == 1  # noqa: SLF001
    assert len(view.rows) == 1  # noqa: SLF001
    row = view.rows[0]  # noqa: SLF001
    assert row.candidate_profile_id == A_ID
    assert row.vector_rank == 1
    assert row.structured_rank is None
    assert row.keyword_rank is None


def test_empty_corpus_yields_empty_ranking() -> None:
    empty = InMemoryRetrievalRepository(
        profiles=[],
        chunk_vectors=[],
        job_queries={JOB_VERSION_ID: _job()},
    )
    view = asyncio.run(
        preview_ranking(
            empty,
            FakeEmbeddingGateway(dimension=1024),
            JOB_VERSION_ID,
            _config(),
            CandidateFilter(),
            {},
        )
    )
    assert view.total == 0  # noqa: SLF001
    assert view.rows == []  # noqa: SLF001
