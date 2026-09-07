"""Hermetic tests for the MatchRun graph and candidate snapshot (IMP-019).

No database, no model calls: a ``FakeRankingsProvider`` returns a pre-built
``RankingSnapshot`` and the in-memory repositories record the run. The gate
checks four things — node/event ordering, controlled fan-out, single-candidate
failure isolation, and "FAIL still scores".
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from backend.app.agent.models import AgentEvent, AgentEventType, AgentRun, RunStatus
from backend.app.agent.repository import InMemoryAgentRunRepository
from backend.app.match_run.models import MatchRunCandidate, ProcessingStatus
from backend.app.match_run.repository import (
    InMemoryMatchRunCandidateRepository,
    InMemoryMatchRunRepository,
)
from backend.app.match_run.service import MatchRunService
from backend.app.retrieval.models import (
    FusedCandidate,
    HardRuleBundle,
    HardRuleOutcome,
    RankingSnapshot,
    RetrievalConfig,
)

JOB_VERSION_ID = uuid4()
CONFIG = RetrievalConfig(
    structured_weight=1.0,
    keyword_weight=1.0,
    vector_weight=1.0,
    rrf_k=60,
    top_k=10,
    rule_version="v1",
)


def _bundle(overall: HardRuleOutcome) -> HardRuleBundle:
    return HardRuleBundle(rules=[], overall=overall)


def _candidate(profile_id: UUID, order: int, overall: HardRuleOutcome) -> FusedCandidate:
    return FusedCandidate(
        candidate_profile_id=profile_id,
        snapshot_order=order,
        rrf_score=float(10 - order),
        structured_rank=order,
        keyword_rank=None,
        vector_rank=order,
        hard_rule=_bundle(overall),
    )


def _snapshot(fused: list[FusedCandidate]) -> RankingSnapshot:
    return RankingSnapshot(job_version_id=JOB_VERSION_ID, config=CONFIG, fused=fused)


def _provider(snapshot: RankingSnapshot) -> object:
    class _Provider:
        def __init__(self, snap: RankingSnapshot) -> None:
            self._snap = snap

        async def get_snapshot(self, *, job_version_id: UUID) -> RankingSnapshot:
            return self._snap

    return _Provider(snapshot)


def _service(
    snapshot: RankingSnapshot, applications: object | None = None
) -> MatchRunService:
    return MatchRunService(
        run_repository=InMemoryAgentRunRepository(),
        match_run_repository=InMemoryMatchRunRepository(),
        candidate_repository=InMemoryMatchRunCandidateRepository(),
        rankings=_provider(snapshot),  # type: ignore[arg-type]
        applications=applications,  # type: ignore[arg-type]
        concurrency=4,
    )


def _application_ids(fused: list[FusedCandidate]) -> dict[UUID, UUID]:
    return {c.candidate_profile_id: c.candidate_profile_id for c in fused}


def _run(
    fused: list[FusedCandidate],
    *,
    fail_profiles: set[UUID] | None = None,
    concurrency: int = 4,
) -> tuple[AgentRun, list[MatchRunCandidate], list[AgentEvent]]:
    snapshot = _snapshot(fused)
    service = _service(snapshot)
    run, match_run = asyncio.run(
        service.create_match_run(
            job_id=uuid4(),
            job_version_id=JOB_VERSION_ID,
            actor_id=uuid4(),
            retrieval_config={"top_k": 10},
            model_config={"model": "fake"},
            prompt_version="v1",
            rule_version="v1",
        )
    )
    asyncio.run(
        service.execute_match_run(
            run=run,
            match_run=match_run,
            application_ids=_application_ids(fused),
            fail_profiles=fail_profiles,
        )
    )
    candidates = asyncio.run(service._candidates.get_candidates(run.id))  # noqa: SLF001
    events = asyncio.run(service._runs.list_events(run.id))  # noqa: SLF001
    return run, sorted(candidates, key=lambda c: c.snapshot_order), events


def test_match_run_writes_candidate_snapshots_in_order() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    run, candidates, _ = _run(
        [
            _candidate(a, 1, HardRuleOutcome.PASS),
            _candidate(b, 2, HardRuleOutcome.UNKNOWN),
            _candidate(c, 3, HardRuleOutcome.FAIL),
        ]
    )
    assert [candidate.candidate_profile_id for candidate in candidates] == [a, b, c]
    assert [candidate.snapshot_order for candidate in candidates] == [1, 2, 3]
    assert all(
        c.processing_status == ProcessingStatus.COMPLETED for c in candidates
    )
    assert all(c.hard_rule_result_json is not None for c in candidates)
    # The run itself reached a terminal success state.
    assert run.status == RunStatus.COMPLETED


def test_match_run_resolves_durable_application_ids() -> None:
    asyncio.run(_match_run_resolves_durable_application_ids())


async def _match_run_resolves_durable_application_ids() -> None:
    profile_ids = [uuid4(), uuid4()]
    application_ids = {profile_id: uuid4() for profile_id in profile_ids}

    class _Applications:
        requested_job_id: UUID | None = None
        requested_profile_ids: list[UUID] = []

        async def get_or_create(
            self, *, job_id: UUID, profile_ids: list[UUID]
        ) -> dict[UUID, UUID]:
            self.requested_job_id = job_id
            self.requested_profile_ids = profile_ids
            return application_ids

    applications = _Applications()
    snapshot = _snapshot(
        [
            _candidate(profile_id, order, HardRuleOutcome.PASS)
            for order, profile_id in enumerate(profile_ids, start=1)
        ]
    )
    service = _service(snapshot, applications)
    job_id = uuid4()
    run, match_run = await service.create_match_run(
        job_id=job_id,
        job_version_id=JOB_VERSION_ID,
        actor_id=uuid4(),
        retrieval_config={"top_k": 10},
        model_config={"model": "fake"},
        prompt_version="v1",
        rule_version="v1",
    )

    await service.execute_match_run(run=run, match_run=match_run)

    candidates = await service._candidates.get_candidates(run.id)  # noqa: SLF001
    assert applications.requested_job_id == job_id
    assert applications.requested_profile_ids == profile_ids
    assert {
        candidate.candidate_profile_id: candidate.application_id
        for candidate in candidates
    } == application_ids


def test_node_and_event_sequence_is_ordered() -> None:
    run, _, events = _run([_candidate(uuid4(), 1, HardRuleOutcome.PASS)])
    sequences = [event.sequence for event in events]
    assert sequences == sorted(sequences)
    assert sequences[0] == 0
    node_names = {
        event.node for event in events if event.node and event.node in {
            "parse_job", "retrieve_candidates", "snapshot_candidates",
            "hard_rule_evaluate", "score_with_evidence", "aggregate_run",
        }
    }
    assert node_names == {
        "parse_job", "retrieve_candidates", "snapshot_candidates",
        "hard_rule_evaluate", "score_with_evidence", "aggregate_run",
    }
    terminal = [event for event in events if event.event_type == AgentEventType.RUN_COMPLETED]
    assert len(terminal) == 1


def test_hard_rule_fail_still_scores() -> None:
    fail_profile = uuid4()
    _, candidates, _ = _run(
        [
            _candidate(uuid4(), 1, HardRuleOutcome.PASS),
            _candidate(fail_profile, 2, HardRuleOutcome.FAIL),
        ]
    )
    row = next(c for c in candidates if c.candidate_profile_id == fail_profile)
    # Hard-rule FAIL is NOT a candidate failure: it still completes and is retained.
    assert row.processing_status == ProcessingStatus.COMPLETED
    assert row.hard_rule_result_json is not None
    assert row.hard_rule_result_json["overall"] == "FAIL"


def test_single_candidate_failure_is_isolated() -> None:
    bad = uuid4()
    good1, good2 = uuid4(), uuid4()
    run, candidates, _ = _run(
        [
            _candidate(good1, 1, HardRuleOutcome.PASS),
            _candidate(bad, 2, HardRuleOutcome.PASS),
            _candidate(good2, 3, HardRuleOutcome.PASS),
        ],
        fail_profiles={bad},
    )
    by_id = {c.candidate_profile_id: c for c in candidates}
    assert by_id[bad].processing_status == ProcessingStatus.FAILED
    assert by_id[bad].error_code == "CANDIDATE_SCORING_FAILED"
    assert by_id[good1].processing_status == ProcessingStatus.COMPLETED
    assert by_id[good2].processing_status == ProcessingStatus.COMPLETED
    # One isolated failure does not fail the whole run (others completed).
    assert run.status == RunStatus.COMPLETED


def test_all_candidates_failed_runs_failed() -> None:
    ids = [uuid4(), uuid4(), uuid4()]
    run, candidates, _ = _run(
        [_candidate(pid, i + 1, HardRuleOutcome.PASS) for i, pid in enumerate(ids)],
        fail_profiles=set(ids),
    )
    assert all(c.processing_status == ProcessingStatus.FAILED for c in candidates)
    assert run.status == RunStatus.FAILED


def test_empty_top_k_completes() -> None:
    run, candidates, events = _run([])
    assert candidates == []
    assert run.status == RunStatus.COMPLETED
    assert any(event.event_type == AgentEventType.RUN_COMPLETED for event in events)


def test_concurrent_fan_out_processes_every_candidate() -> None:
    ids = [uuid4() for _ in range(20)]
    run, candidates, _ = _run(
        [_candidate(pid, i + 1, HardRuleOutcome.PASS) for i, pid in enumerate(ids)],
        concurrency=4,
    )
    assert len(candidates) == 20
    assert {c.candidate_profile_id for c in candidates} == set(ids)
    assert all(c.processing_status == ProcessingStatus.COMPLETED for c in candidates)
    assert run.status == RunStatus.COMPLETED
