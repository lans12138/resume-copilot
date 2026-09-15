"""IMP-014 three-way recall tests (D12 structured/keyword, D13 vector).

Recallers are pure given their projections, so we drive them with in-memory
projections and the deterministic ``FakeEmbeddingGateway``. The bundle from
``RetrievalService.run_recall`` must be byte-for-byte reproducible across runs
for fixed inputs.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from backend.app.infrastructure.embedding import FakeEmbeddingGateway
from backend.app.retrieval.keyword import KeywordRecaller
from backend.app.retrieval.models import (
    ChannelName,
    ChunkVector,
    JobQuery,
    ReadyProfile,
    RetrievalQuery,
)
from backend.app.retrieval.repository import InMemoryRetrievalRepository
from backend.app.retrieval.service import RetrievalService
from backend.app.retrieval.structured import StructuredRecaller
from backend.app.retrieval.vector import VectorRecaller, cosine_similarity

JOB_ID = uuid4()


def _profile(
    profile_id: UUID,
    *,
    skills: list[str] | None = None,
    years: float | None = None,
    edu: str | None = None,
    profile_json: dict[str, object] | None = None,
) -> ReadyProfile:
    return ReadyProfile(
        profile_id=profile_id,
        normalized_skills=skills or [],
        years_experience=years,
        education_level=edu,
        profile_json=profile_json or {},
    )


def _job(
    *,
    required: list[str] | None = None,
    preferred: list[str] | None = None,
    min_years: float | None = None,
    edu: str | None = None,
    description: str = "",
) -> JobQuery:
    return JobQuery(
        job_version_id=JOB_ID,
        required_skills=required or [],
        preferred_skills=preferred or [],
        min_years=min_years,
        required_education=edu,
        description_text=description,
        requirements_json={},
    )


# --- Structured channel (D12) -------------------------------------------------


def test_structured_full_required_match_ranks_first() -> None:
    p1 = _profile(uuid4(), skills=["python", "fastapi"])
    p2 = _profile(uuid4(), skills=["python"])
    job = _job(required=["python", "fastapi"])
    hits = StructuredRecaller().recall(job, [p2, p1])

    assert [hit.profile_id for hit in hits] == [p1.profile_id, p2.profile_id]
    assert hits[0].score == pytest.approx(0.4)  # full required match = required weight
    assert hits[0].rank == 1


def test_structured_years_partial_score() -> None:
    p = _profile(uuid4(), years=3.0)
    job = _job(min_years=6.0)
    hits = StructuredRecaller().recall(job, [p])

    assert hits[0].score == pytest.approx(0.2 * 0.5)  # 0.1


def test_structured_education_meets_and_below() -> None:
    meets = _profile(uuid4(), edu="硕士")
    below = _profile(uuid4(), edu="本科")
    job = _job(edu="硕士")
    hits = {hit.profile_id: hit.score for hit in StructuredRecaller().recall(job, [below, meets])}

    assert hits[meets.profile_id] == pytest.approx(0.2)
    assert hits[below.profile_id] == pytest.approx(0.2 * (3 / 4))


def test_structured_unknown_education_does_not_crash() -> None:
    p = _profile(uuid4(), edu="unknown-level")
    job = _job(edu="硕士")
    hits = StructuredRecaller().recall(job, [p])

    assert hits[0].score == pytest.approx(0.0)


def test_structured_stable_tie_break_by_profile_id() -> None:
    low = UUID("00000000-0000-0000-0000-000000000001")
    high = UUID("00000000-0000-0000-0000-000000000002")
    job = _job(required=["python"])
    hits = StructuredRecaller().recall(
        job, [_profile(high, skills=["python"]), _profile(low, skills=["python"])]
    )

    assert [hit.profile_id for hit in hits] == [low, high]


def test_structured_skill_alias_collapses_synonyms() -> None:
    # requirement "后端" canonicalizes to "backend"; candidate stores "backend"
    p = _profile(uuid4(), skills=["backend"])
    job = _job(required=["后端"])
    hits = StructuredRecaller().recall(job, [p])

    assert hits[0].score == pytest.approx(0.4)


def test_structured_no_requirement_gives_zero_required_component() -> None:
    p = _profile(uuid4(), skills=["python"])
    job = _job()  # no required, no preferred, no years, no edu
    hits = StructuredRecaller().recall(job, [p])

    assert hits[0].score == pytest.approx(0.0)


# --- Keyword channel (D12) ----------------------------------------------------


def test_keyword_coverage_ranks_by_overlap() -> None:
    strong = _profile(
        uuid4(),
        skills=["python", "fastapi"],
        profile_json={"summary": "built apis with postgres"},
    )
    weak = _profile(uuid4(), skills=["python"], profile_json={"summary": "hello world"})
    job = _job(required=["python", "fastapi"], description="postgres apis")
    hits = KeywordRecaller().recall(job, [weak, strong])

    assert [hit.profile_id for hit in hits] == [strong.profile_id, weak.profile_id]
    assert hits[0].score > hits[1].score


def test_keyword_no_overlap_excluded() -> None:
    p = _profile(uuid4(), skills=["rust"], profile_json={"summary": "systems programming"})
    job = _job(required=["python"], description="web backend")
    hits = KeywordRecaller().recall(job, [p])

    assert hits == []


def test_keyword_empty_job_keywords_returns_empty() -> None:
    p = _profile(uuid4(), skills=["python"])
    job = _job(description="")  # no skills, no description
    hits = KeywordRecaller().recall(job, [p])

    assert hits == []


def test_keyword_alias_token_match() -> None:
    p = _profile(uuid4(), skills=["backend"])
    job = _job(required=["后端"])
    hits = KeywordRecaller().recall(job, [p])

    assert hits[0].score > 0.0


# --- Vector channel (D13) ----------------------------------------------------


def test_vector_max_similarity_aggregation_and_ranking() -> None:
    job_emb = [1.0, 0.0, 0.0, 0.0]
    p1 = uuid4()
    p2 = uuid4()
    chunks = [
        ChunkVector(p1, [1.0, 0.0, 0.0, 0.0]),  # sim 1.0
        ChunkVector(p1, [0.0, 1.0, 0.0, 0.0]),  # sim 0.0
        ChunkVector(p2, [0.7071, 0.7071, 0.0, 0.0]),  # sim ~0.707
    ]
    hits = VectorRecaller().recall(job_emb, chunks)

    assert [hit.profile_id for hit in hits] == [p1, p2]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[1].score == pytest.approx(0.7071, abs=1e-3)


def test_vector_profile_without_embedding_absent() -> None:
    p1 = uuid4()
    p2 = uuid4()
    chunks = [ChunkVector(p1, [1.0, 0.0])]
    hits = VectorRecaller().recall([1.0, 0.0], chunks)

    assert [hit.profile_id for hit in hits] == [p1]
    assert p2 not in [hit.profile_id for hit in hits]


def test_cosine_similarity_dimension_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


# --- run_recall integration --------------------------------------------------


def _build_repo() -> InMemoryRetrievalRepository:
    p1 = uuid4()
    p2 = uuid4()
    profiles = [
        _profile(
            p1,
            skills=["python", "fastapi"],
            years=5.0,
            edu="本科",
            profile_json={"summary": "python backend with fastapi"},
        ),
        _profile(
            p2,
            skills=["java"],
            years=2.0,
            edu="大专",
            profile_json={"summary": "java spring services"},
        ),
    ]
    chunk_vectors = [
        ChunkVector(p1, [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
        ChunkVector(p2, [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
    ]
    job_query = _job(
        required=["python", "fastapi"],
        preferred=["postgres"],
        min_years=3.0,
        edu="本科",
        description="python backend fastapi postgres",
    )
    return InMemoryRetrievalRepository(
        profiles=profiles,
        chunk_vectors=chunk_vectors,
        job_queries={JOB_ID: job_query},
    )


def test_run_recall_reproducible_across_runs() -> None:
    repo = _build_repo()
    gateway = FakeEmbeddingGateway(dimension=8)
    query = RetrievalQuery(job_version_id=JOB_ID, top_k=10, rule_version="v1")

    first = asyncio.run(RetrievalService(repo, gateway).run_recall(query))
    second = asyncio.run(RetrievalService(repo, gateway).run_recall(query))

    assert first == second
    assert first.structured and first.keyword and first.vector
    assert first.structured[0].profile_id == repo.profiles[0].profile_id


def test_run_recall_channel_filter() -> None:
    repo = _build_repo()
    gateway = FakeEmbeddingGateway(dimension=8)
    query = RetrievalQuery(
        job_version_id=JOB_ID,
        top_k=10,
        rule_version="v1",
        channels=(ChannelName.STRUCTURED,),
    )

    bundle = asyncio.run(RetrievalService(repo, gateway).run_recall(query))

    assert bundle.structured and not bundle.keyword and not bundle.vector
