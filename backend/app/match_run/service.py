"""MatchRun graph orchestration and lifecycle service (IMP-019).

``MatchRunService`` drives the job-level batch analysis graph defined in detailed
design §10.2:

~~~text
START -> parse_job -> retrieve_candidates -> snapshot_candidates
      -> fan_out_candidate (per candidate, fixed node group)
            -> hard_rule_evaluate -> score_with_evidence
      -> aggregate_run -> END
~~~

The graph has *no* interrupt: MatchRun is a pure batch analysis with no side
effects and no approval, so it runs to a terminal state in one pass. Each node
appends a strictly-ordered ``AgentEvent`` via the ``AgentRunRepository`` (the
same spine every run shares, IMP-018). The fan-out runs each candidate's fixed
node group under a bounded concurrency semaphore.

Two invariants the gate checks:

* **Single-candidate failure isolation** — if a candidate's node group raises,
  that candidate's ``MatchRunCandidate`` is flipped to ``FAILED`` (with an
  ``error_code``) and recorded in ``failed_candidate_ids``; every *other*
  candidate continues. Only an all-failed run or an invalid snapshot fails the
  whole ``MatchRun``.
* **FAIL still scores** — a hard-rule ``FAIL``/``UNKNOWN`` verdict is *not* a
  candidate failure: the candidate still reaches ``COMPLETED`` with its verdict
  recorded. Hiding ``FAIL``/``UNKNOWN`` candidates is a presentation concern
  owned by the candidate list UI (§8.4, §9.3), never by the run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Set
from typing import Protocol
from uuid import UUID, uuid4

from backend.app.agent.models import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    RunStatus,
    RunType,
)
from backend.app.agent.repository import AgentRunRepository
from backend.app.match_run.models import MatchRun, MatchRunCandidate, ProcessingStatus
from backend.app.match_run.repository import (
    MatchRunCandidateRepository,
    MatchRunRepository,
)
from backend.app.reports.service import EvidenceProvider, ReportService
from backend.app.retrieval.models import FusedCandidate, HardRuleBundle, RankingSnapshot


class RankingsProvider(Protocol):
    """Supplies the frozen ranking snapshot a MatchRun consumes (§10.2)."""

    async def get_snapshot(self, *, job_version_id: UUID) -> RankingSnapshot: ...


class MatchRunService:
    """Create and execute job-level MatchRun analyses."""

    def __init__(
        self,
        run_repository: AgentRunRepository,
        match_run_repository: MatchRunRepository,
        candidate_repository: MatchRunCandidateRepository,
        rankings: RankingsProvider,
        *,
        concurrency: int = 4,
    ) -> None:
        self._runs = run_repository
        self._match_runs = match_run_repository
        self._candidates = candidate_repository
        self._rankings = rankings
        self._concurrency = max(1, concurrency)

    async def create_match_run(
        self,
        *,
        job_id: UUID,
        job_version_id: UUID,
        actor_id: UUID,
        retrieval_config: dict[str, object],
        model_config: dict[str, object],
        prompt_version: str,
        rule_version: str,
        application_ids: Mapping[UUID, UUID],
    ) -> tuple[AgentRun, MatchRun]:
        """Insert the CREATED run + MatchRun header and its first event."""
        run = AgentRun(
            id=uuid4(),
            thread_id=uuid4().hex,
            run_type=RunType.MATCH,
            status=RunStatus.CREATED,
            attempt=1,
            next_event_sequence=0,
            version=1,
            config_snapshot_json={
                "job_version_id": str(job_version_id),
                "retrieval_config": retrieval_config,
                "rule_version": rule_version,
            },
        )
        await self._runs.save_run(run)
        await self._append(
            run,
            AgentEventType.RUN_CREATED,
            node=None,
            status=RunStatus.CREATED.value,
            message_key="match_run.created",
            safe_payload={"job_id": str(job_id), "job_version_id": str(job_version_id)},
        )
        await self._runs.set_status(run.id, RunStatus.RUNNING)

        match_run = MatchRun(
            run_id=run.id,
            job_id=job_id,
            job_version_id=job_version_id,
            retrieval_config_json=retrieval_config,
            model_config_json=model_config,
            prompt_version=prompt_version,
            rule_version=rule_version,
        )
        await self._match_runs.save_match_run(match_run)
        return run, match_run

    async def execute_match_run(
        self,
        *,
        run: AgentRun,
        match_run: MatchRun,
        application_ids: Mapping[UUID, UUID],
        fail_profiles: Set[UUID] | None = None,
        report_service: ReportService | None = None,
        evidence_provider: EvidenceProvider | None = None,
    ) -> MatchRun:
        """Run the full graph for one MatchRun, terminating it on success/failure.

        ``fail_profiles`` is a controlled fault-injection seam: any profile in it
        makes its ``score_with_evidence`` node raise, exercising single-candidate
        failure isolation. Production code never passes it.
        """
        faults = fail_profiles or set()

        await self._node(run, "parse_job", {"job_version_id": str(match_run.job_version_id)})
        snapshot = await self._retrieve_candidates(run, match_run)

        await self._node(run, "snapshot_candidates", {"count": len(snapshot.fused)})
        await self._snapshot_candidates(run, match_run, snapshot, application_ids)

        failed_ids = await self._fan_out(
            run, match_run, snapshot, application_ids, faults
        )

        status = await self._aggregate(run, snapshot, failed_ids)
        # Gate G4: a successful MatchRun persists evidence-backed reports for its
        # COMPLETED candidates. Report generation is optional so IMP-019 callers
        # (and fault-injection tests) are unaffected; a failed run writes none.
        if (
            status == RunStatus.COMPLETED
            and report_service is not None
            and evidence_provider is not None
        ):
            candidates = await self._candidates.get_candidates(run.id)
            await report_service.generate_for_run(
                run=run,
                match_run=match_run,
                candidates=candidates,
                evidence=evidence_provider,
            )
        return match_run

    async def _retrieve_candidates(
        self, run: AgentRun, match_run: MatchRun
    ) -> RankingSnapshot:
        await self._append(
            run,
            AgentEventType.NODE_STARTED,
            node="retrieve_candidates",
            status=RunStatus.RUNNING.value,
            message_key="node.retrieve_candidates.started",
            safe_payload={"job_version_id": str(match_run.job_version_id)},
        )
        snapshot = await self._rankings.get_snapshot(job_version_id=match_run.job_version_id)
        await self._append(
            run,
            AgentEventType.NODE_COMPLETED,
            node="retrieve_candidates",
            status=RunStatus.RUNNING.value,
            message_key="node.retrieve_candidates.completed",
            safe_payload={"candidates": len(snapshot.fused)},
        )
        return snapshot

    async def _snapshot_candidates(
        self,
        run: AgentRun,
        match_run: MatchRun,
        snapshot: RankingSnapshot,
        application_ids: Mapping[UUID, UUID],
    ) -> None:
        rows: list[MatchRunCandidate] = []
        for fused in snapshot.fused:
            rows.append(
                MatchRunCandidate(
                    run_id=match_run.run_id,
                    candidate_profile_id=fused.candidate_profile_id,
                    application_id=application_ids.get(
                        fused.candidate_profile_id, fused.candidate_profile_id
                    ),
                    snapshot_order=fused.snapshot_order,
                    structured_rank=fused.structured_rank,
                    keyword_rank=fused.keyword_rank,
                    vector_rank=fused.vector_rank,
                    structured_score=fused.structured_score,
                    keyword_score=fused.keyword_score,
                    vector_score=fused.vector_score,
                    rrf_score=fused.rrf_score,
                    processing_status=ProcessingStatus.PENDING,
                )
            )
        await self._candidates.save_candidates(rows)

    async def _fan_out(
        self,
        run: AgentRun,
        match_run: MatchRun,
        snapshot: RankingSnapshot,
        application_ids: Mapping[UUID, UUID],
        faults: Set[UUID],
    ) -> list[UUID]:
        failed_ids: list[UUID] = []
        semaphore = asyncio.Semaphore(self._concurrency)

        async def process(fused: FusedCandidate) -> None:
            profile_id = fused.candidate_profile_id
            async with semaphore:
                try:
                    await self._append(
                        run,
                        AgentEventType.NODE_STARTED,
                        node="hard_rule_evaluate",
                        status=RunStatus.RUNNING.value,
                        message_key="node.hard_rule_evaluate.started",
                        safe_payload={"candidate_profile_id": str(profile_id)},
                    )
                    hard_rule_json = _hard_rule_json(fused.hard_rule)
                    await self._append(
                        run,
                        AgentEventType.NODE_COMPLETED,
                        node="hard_rule_evaluate",
                        status=RunStatus.RUNNING.value,
                        message_key="node.hard_rule_evaluate.completed",
                        safe_payload={
                            "candidate_profile_id": str(profile_id),
                            "overall": hard_rule_json["overall"],
                        },
                    )
                    await self._append(
                        run,
                        AgentEventType.NODE_STARTED,
                        node="score_with_evidence",
                        status=RunStatus.RUNNING.value,
                        message_key="node.score_with_evidence.started",
                        safe_payload={"candidate_profile_id": str(profile_id)},
                    )
                    if profile_id in faults:
                        raise RuntimeError("injected scoring fault")
                    # FAIL/UNKNOWN still scores: the candidate is finalized as
                    # COMPLETED with its verdict recorded (never dropped).
                    await self._candidates.mark_candidate_completed(
                        run_id=match_run.run_id,
                        profile_id=profile_id,
                        hard_rule_result_json=hard_rule_json,
                    )
                    await self._append(
                        run,
                        AgentEventType.NODE_COMPLETED,
                        node="score_with_evidence",
                        status=RunStatus.RUNNING.value,
                        message_key="node.score_with_evidence.completed",
                        safe_payload={"candidate_profile_id": str(profile_id)},
                    )
                except Exception as exc:  # noqa: BLE001 - isolation boundary
                    await self._candidates.mark_candidate_failed(
                        run_id=match_run.run_id,
                        profile_id=profile_id,
                        error_code="CANDIDATE_SCORING_FAILED",
                    )
                    await self._append(
                        run,
                        AgentEventType.NODE_COMPLETED,
                        node="score_with_evidence",
                        status=ProcessingStatus.FAILED.value,
                        message_key="node.score_with_evidence.failed",
                        safe_payload={
                            "candidate_profile_id": str(profile_id),
                            "error_code": "CANDIDATE_SCORING_FAILED",
                            "error": str(exc),
                        },
                    )
                    failed_ids.append(profile_id)

        await asyncio.gather(*(process(fused) for fused in snapshot.fused))
        return failed_ids

    async def _aggregate(
        self, run: AgentRun, snapshot: RankingSnapshot, failed_ids: list[UUID]
    ) -> RunStatus:
        await self._append(
            run,
            AgentEventType.NODE_STARTED,
            node="aggregate_run",
            status=RunStatus.RUNNING.value,
            message_key="node.aggregate_run.started",
            safe_payload={},
        )
        if not snapshot.fused:
            # No candidates hit: allowed to complete with an empty summary.
            status = RunStatus.COMPLETED
        elif failed_ids and len(failed_ids) == len(snapshot.fused):
            status = RunStatus.FAILED
        else:
            status = RunStatus.COMPLETED
        await self._append(
            run,
            AgentEventType.NODE_COMPLETED,
            node="aggregate_run",
            status=status.value,
            message_key="node.aggregate_run.completed",
            safe_payload={
                "failed_candidate_ids": [str(p) for p in failed_ids],
                "total": len(snapshot.fused),
            },
        )
        if status == RunStatus.FAILED:
            await self._append(
                run,
                AgentEventType.RUN_FAILED,
                node=None,
                status=RunStatus.FAILED.value,
                message_key="match_run.failed",
                safe_payload={"reason": "all_candidates_failed"},
            )
            await self._runs.set_status(run.id, RunStatus.FAILED, finished=True)
        else:
            await self._append(
                run,
                AgentEventType.RUN_COMPLETED,
                node=None,
                status=RunStatus.COMPLETED.value,
                message_key="match_run.completed",
                safe_payload={"failed_candidate_ids": [str(p) for p in failed_ids]},
            )
            await self._runs.set_status(run.id, RunStatus.COMPLETED, finished=True)
        return status

    async def _node(
        self, run: AgentRun, name: str, payload: dict[str, object]
    ) -> None:
        await self._append(
            run,
            AgentEventType.NODE_STARTED,
            node=name,
            status=RunStatus.RUNNING.value,
            message_key=f"node.{name}.started",
            safe_payload=payload,
        )
        await self._append(
            run,
            AgentEventType.NODE_COMPLETED,
            node=name,
            status=RunStatus.RUNNING.value,
            message_key=f"node.{name}.completed",
            safe_payload=payload,
        )

    async def _append(
        self,
        run: AgentRun,
        event_type: AgentEventType,
        *,
        node: str | None,
        status: str,
        message_key: str,
        safe_payload: dict[str, object],
    ) -> AgentEvent:
        return await self._runs.append_event(
            run_id=run.id,
            run_type=run.run_type,
            event_type=event_type,
            node=node,
            status=status,
            message_key=message_key,
            safe_payload=safe_payload,
        )


def _hard_rule_json(bundle: HardRuleBundle | None) -> dict[str, object]:
    """Serialize a hard-rule verdict to the snapshot JSON column.

    A missing bundle is reported as ``UNKNOWN`` (never guessed ``FAIL``, §8.4).
    """
    if bundle is None:
        return {"overall": "UNKNOWN", "rules": []}
    return {
        "overall": bundle.overall.value,
        "rules": [
            {
                "rule_id": rule.rule_id.value,
                "result": rule.result.value,
                "reason_code": rule.reason_code,
                "observed_value": rule.observed_value,
                "required_value": rule.required_value,
            }
            for rule in bundle.rules
        ],
    }
