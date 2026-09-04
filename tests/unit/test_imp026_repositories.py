"""Hermetic tests for the IMP-026 repository query methods that back the new
Run / Approval / Interview endpoints (no database, no model calls).

These cover the list/lookup methods added so the browser can enumerate runs and
resolve an interview from its parent run: ``MatchRunRepository.list_by_job``,
``InterviewRepository.get_by_run``, and ``JobApplicationRepository.list_by_job``.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from backend.app.interviews.models import Interview
from backend.app.interviews.repository import InMemoryInterviewRepository
from backend.app.job_applications.models import JobApplication
from backend.app.job_applications.repository import InMemoryJobApplicationRepository
from backend.app.match_run.models import MatchRun
from backend.app.match_run.repository import InMemoryMatchRunRepository


def _match_run(job_id: object) -> MatchRun:
    return MatchRun(
        run_id=uuid4(),
        job_id=job_id,  # type: ignore[arg-type]
        job_version_id=uuid4(),
        retrieval_config_json={},
        model_config_json={},
        prompt_version="v1",
        rule_version="v1",
    )


def test_match_run_repository_list_by_job_filters_other_jobs() -> None:
    repo = InMemoryMatchRunRepository()
    job_a, job_b = uuid4(), uuid4()
    run_a = _match_run(job_a)
    run_b = _match_run(job_b)
    asyncio.run(repo.save_match_run(run_a))
    asyncio.run(repo.save_match_run(run_b))

    result = asyncio.run(repo.list_by_job(job_a))

    assert [r.run_id for r in result] == [run_a.run_id]


def test_interview_repository_get_by_run() -> None:
    repo = InMemoryInterviewRepository()
    run_id = uuid4()
    interview = Interview(
        application_id=uuid4(),
        run_id=run_id,
        approval_id=uuid4(),
        external_schedule_id="ext-1",
        schedule_json={
            "application_id": str(uuid4()),
            "duration_minutes": 45,
            "timezone": "UTC",
            "interviewer_label": "Hiring Manager",
        },
    )
    asyncio.run(repo.save_interview(interview))

    found = asyncio.run(repo.get_by_run(run_id))
    assert found is not None
    assert found.external_schedule_id == "ext-1"
    assert asyncio.run(repo.get_by_run(uuid4())) is None


def test_job_application_repository_list_by_job() -> None:
    repo = InMemoryJobApplicationRepository()
    job_a, job_b = uuid4(), uuid4()
    app_a = JobApplication(id=uuid4(), job_id=job_a, candidate_id=uuid4())
    app_b = JobApplication(id=uuid4(), job_id=job_b, candidate_id=uuid4())
    asyncio.run(repo.save_application(app_a))
    asyncio.run(repo.save_application(app_b))

    result = asyncio.run(repo.list_by_job(job_a))

    assert [a.id for a in result] == [app_a.id]
