"""Synthetic demo data seeder for resume-copilot (IMP-030).

Populates a *fresh* database with self-contained synthetic data so the full
recruiting demo (MatchRun batch analysis -> ApplicationRun with dual approval
-> Interview) can run with ``MOCK_MODEL_MODE=true`` and no external services.

Design notes
------------
* We write through the SQLAlchemy ORM models directly (the repository ``save``
  methods are thin ``session.add`` wrappers), so the script stays correct even
  when a high-level service would otherwise publish a Celery task.
* All foreign-key chains are honoured explicitly:
  - an ACTIVE ``Job`` MUST carry ``current_version_id`` (ck_jobs_published_version
    + fk_jobs_current_version_belongs_to_job).
  - ``CandidateProfile.document_id`` and every ``EvidenceChunk.document_id`` must
    reference the same ``resume_documents.id`` (composite FK on evidence_chunks).
  - only one READY profile per candidate (partial unique index).
* Embeddings are deterministic unit vectors derived from the chunk text. They are
  *synthetic* but let vector recall run offline; a real deployment re-embeds via
  the configured gateway.

Run inside the backend container (so ``postgres`` resolves and ``backend`` is
importable):

    docker compose --profile tools run --rm --build seed
    docker compose --profile tools run --rm --build seed -- --reset

Or locally after ``pip install -r requirements.lock`` and a reachable Postgres:

    python scripts/seed_demo_data.py
    python scripts/seed_demo_data.py --reset
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import random
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

# Make ``backend`` importable whether run from repo root or /workspace.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from backend.app.auth.models import User, UserRole  # noqa: E402
from backend.app.auth.passwords import password_service  # noqa: E402
from backend.app.candidates.models import (  # noqa: E402
    Candidate,
    CandidateProfile,
    CandidateProfileStatus,
    EvidenceChunk,
)
from backend.app.core.settings import get_settings  # noqa: E402
from backend.app.documents.models import DocumentStatus, ResumeDocument  # noqa: E402
from backend.app.infrastructure.database import (  # noqa: E402
    SqlAlchemyUnitOfWork,
    build_engine,
    build_session_factory,
)
from backend.app.job_applications.models import (  # noqa: E402
    ApplicationStatus,
    JobApplication,
)
from backend.app.jobs.models import Job, JobAssignment, JobStatus, JobVersion  # noqa: E402

DEMO_USERNAME = "hr.demo"
# JobAssignment only constrains HIRING_MANAGER: `JobService.get_authorized`
# returns any job to an HR actor without consulting assignments, and
# `grant_assignment` refuses any other role. So the resource-level authorization
# path — and with it the SSE revocation scenario (detailed design §19.6.5, and
# the assignment API itself) — needs a manager who can actually be assigned.
DEMO_MANAGER_USERNAME = "hm.demo"
DEMO_PASSWORD = "demo-password-123"
DEMO_JOB_TITLE = "[DEMO] 高级后端工程师（Go / Python）"
DEMO_CANDIDATE_PREFIX = "Demo "

EMBEDDING_DIM = 1024


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_embedding(text: str) -> list[float]:
    """Deterministic unit vector so offline vector recall is reproducible."""
    rng = random.Random(text.encode("utf-8"))
    vec = [rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@dataclass
class CandidateSeed:
    name: str
    email: str
    years: float
    education: str
    skills: list[str]
    summary: str
    chunks: list[tuple[str, str]]


def _candidate_payloads() -> list[CandidateSeed]:
    """Five synthetic candidates with realistic, varied profiles."""
    return [
        CandidateSeed(
            name="Demo 林知远",
            email="zhiyuan.lin@example.com",
            years=6.5,
            education="硕士",
            skills=["Go", "gRPC", "PostgreSQL", "Kubernetes", "Redis"],
            summary="6 年后端经验，主导过高并发订单系统重构，擅长 Go 与分布式服务治理。",
            chunks=[
                ("工作经历", "2021-至今 某电商 高级后端工程师，负责交易链路，QPS 峰值 3 万。"),
                ("项目经验", "主导订单中心拆分，拆成 12 个 Go 微服务，P99 延迟降 40%。"),
                ("技能", "精通 Go、gRPC、PostgreSQL 调优与 K8s 编排。"),
            ],
        ),
        CandidateSeed(
            name="Demo 沈曜",
            email="yao.shen@example.com",
            years=4.0,
            education="本科",
            skills=["Python", "FastAPI", "PostgreSQL", "Celery", "React"],
            summary="4 年后端开发，熟悉 Python 异步服务与数据管道，有招聘系统开发背景。",
            chunks=[
                ("工作经历", "2022-至今 某 SaaS 公司 后端工程师，使用 FastAPI 构建招聘管理 API。"),
                ("项目经验", "设计简历解析流水线，结合规则与模型抽取结构化字段，准确率达 92%。"),
                ("技能", "熟练使用 Python、FastAPI、Celery 与 PostgreSQL。"),
            ],
        ),
        CandidateSeed(
            name="Demo 苏微",
            email="wei.su@example.com",
            years=8.0,
            education="硕士",
            skills=["Java", "Spring", "Kafka", "MySQL", "微服务"],
            summary="8 年后端架构经验，主导过大规模微服务平台，关注可观测性与稳定性。",
            chunks=[
                ("工作经历", "2018-至今 某金融科技 技术专家，负责核心账务系统。"),
                ("项目经验", "搭建基于 Kafka 的事件驱动架构，日均处理 2 亿笔交易。"),
                ("技能", "精通 Java、Spring 生态、Kafka 与 MySQL 高可用。"),
            ],
        ),
        CandidateSeed(
            name="Demo 周屿",
            email="yu.zhou@example.com",
            years=2.5,
            education="本科",
            skills=["Python", "Django", "MySQL", "Docker"],
            summary="2.5 年初级后端，参与内部工具开发，学习能力强，有全栈实习经历。",
            chunks=[
                ("工作经历", "2023-至今 某创业公司 后端开发，负责内部运营后台。"),
                ("项目经验", "搭建自动化报表系统，减少人工统计工时约 60%。"),
                ("技能", "熟悉 Python、Django、MySQL 与 Docker 基础。"),
            ],
        ),
        CandidateSeed(
            name="Demo 何笙",
            email="sheng.he@example.com",
            years=5.0,
            education="本科",
            skills=["Go", "Python", "PostgreSQL", "Redis", "Kafka"],
            summary="5 年全栈偏向后端，做过爬虫与推荐系统，熟悉 Go 与 Python 双栈。",
            chunks=[
                ("工作经历", "2020-至今 某内容平台 后端工程师，负责推荐召回服务。"),
                ("项目经验", "实现多路召回融合，结合向量检索与规则过滤提升点击率 15%。"),
                ("技能", "熟练 Go 与 Python，熟悉 PostgreSQL、Redis 与 Kafka。"),
            ],
        ),
    ]


async def _reset(session: AsyncSession) -> None:
    """Remove demo rows by marker so the seed is re-runnable."""
    demo_candidates = select(Candidate.id).where(
        Candidate.display_name.like(f"{DEMO_CANDIDATE_PREFIX}%")
    )
    demo_job_ids = select(Job.id).where(
        Job.title == DEMO_JOB_TITLE
    )

    chunk_ids = select(EvidenceChunk.id).where(
        EvidenceChunk.candidate_profile_id.in_(
            select(CandidateProfile.id).where(
                CandidateProfile.candidate_id.in_(demo_candidates)
            )
        )
    )
    await session.execute(delete(EvidenceChunk).where(EvidenceChunk.id.in_(chunk_ids)))
    await session.execute(
        delete(JobApplication).where(JobApplication.job_id.in_(demo_job_ids))
    )
    await session.execute(
        delete(CandidateProfile).where(
            CandidateProfile.candidate_id.in_(demo_candidates)
        )
    )
    await session.execute(
        delete(ResumeDocument).where(
            ResumeDocument.original_filename.like("demo-%")
        )
    )
    await session.execute(
        delete(Candidate).where(
            Candidate.display_name.like(f"{DEMO_CANDIDATE_PREFIX}%")
        )
    )
    await session.execute(
        delete(JobAssignment).where(JobAssignment.job_id.in_(demo_job_ids))
    )
    await session.execute(delete(Job).where(Job.title == DEMO_JOB_TITLE))
    await session.execute(
        delete(User).where(User.username.in_([DEMO_USERNAME, DEMO_MANAGER_USERNAME]))
    )
    await session.commit()


async def seed(reset: bool) -> None:
    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        session = uow.session

        if reset:
            await _reset(session)
            print("[seed] cleared previous demo data")

        # 1) HR user (idempotent: reuse if already present)
        user = await session.scalar(
            select(User).where(User.username == DEMO_USERNAME)
        )
        if user is None:
            user = User(
                username=DEMO_USERNAME,
                password_hash=password_service.hash(DEMO_PASSWORD),
                role=UserRole.HR,
                is_active=True,
            )
            session.add(user)
            await session.flush()
            print(f"[seed] created HR user '{DEMO_USERNAME}'")
        else:
            print(f"[seed] HR user '{DEMO_USERNAME}' already exists")

        # 1b) Hiring manager (idempotent: reuse if already present).
        manager = await session.scalar(
            select(User).where(User.username == DEMO_MANAGER_USERNAME)
        )
        if manager is None:
            manager = User(
                username=DEMO_MANAGER_USERNAME,
                password_hash=password_service.hash(DEMO_PASSWORD),
                role=UserRole.HIRING_MANAGER,
                is_active=True,
            )
            session.add(manager)
            await session.flush()
            print(f"[seed] created HIRING_MANAGER user '{DEMO_MANAGER_USERNAME}'")
        else:
            print(f"[seed] HIRING_MANAGER user '{DEMO_MANAGER_USERNAME}' already exists")

        # 2) ACTIVE job + version + assignment (idempotent)
        job = await session.scalar(select(Job).where(Job.title == DEMO_JOB_TITLE))
        if job is None:
            job = Job(
                title=DEMO_JOB_TITLE,
                status=JobStatus.DRAFT,
                created_by=user.id,
            )
            session.add(job)
            await session.flush()

            description = (
                "负责招聘平台核心后端服务，使用 Go / Python 构建高并发 API，"
                "要求熟悉 PostgreSQL、Redis、消息队列与微服务架构，有向量检索经验者优先。"
            )
            job_version = JobVersion(
                job_id=job.id,
                version_no=1,
                description_text=description,
                requirements_json={
                    "required_skills": ["Go", "Python", "PostgreSQL"],
                    "preferred_skills": ["Redis", "Kafka", "Kubernetes"],
                    "minimum_years_experience": 3,
                    "education_level": "本科",
                },
                content_sha256=_sha256(description),
                created_by=user.id,
            )
            session.add(job_version)
            await session.flush()

            job.current_version_id = job_version.id
            job.status = JobStatus.ACTIVE

            assignment = JobAssignment(
                job_id=job.id,
                user_id=user.id,
                assigned_by=user.id,
                assigned_at=datetime.now(UTC),
            )
            session.add(assignment)
            print(f"[seed] created ACTIVE job '{DEMO_JOB_TITLE}'")
        else:
            print(f"[seed] job '{DEMO_JOB_TITLE}' already exists")

        # 2b) Ensure the manager holds an active assignment to the demo job.
        # Deliberately outside the create branch: a database seeded before the
        # manager existed still needs the row, and re-running the seed must not
        # duplicate it (the partial unique index on active assignments would
        # reject that anyway).
        existing_assignment = await session.scalar(
            select(JobAssignment).where(
                JobAssignment.job_id == job.id,
                JobAssignment.user_id == manager.id,
                JobAssignment.revoked_at.is_(None),
            )
        )
        if existing_assignment is None:
            session.add(
                JobAssignment(
                    job_id=job.id,
                    user_id=manager.id,
                    assigned_by=user.id,
                    assigned_at=datetime.now(UTC),
                )
            )
            print(f"[seed] assigned '{DEMO_MANAGER_USERNAME}' to the demo job")

        # 3) Candidates with READY profile + evidence chunks
        existing = set(
            row[0]
            for row in (
                await session.execute(
                    select(Candidate.display_name).where(
                        Candidate.display_name.like(f"{DEMO_CANDIDATE_PREFIX}%")
                    )
                )
            ).all()
        )

        created = 0
        for payload in _candidate_payloads():
            if payload.name in existing:
                continue

            candidate = Candidate(
                display_name=payload.name,
                normalized_email_hash=_sha256(payload.email),
            )
            session.add(candidate)
            await session.flush()

            document = ResumeDocument(
                original_filename=f"demo-{payload.email}.pdf",
                storage_key=f"demo/{payload.email}.pdf",
                media_type="application/pdf",
                size_bytes=48213,
                content_sha256=_sha256(f"demo-resume-{payload.email}"),
                status=DocumentStatus.READY,
                uploaded_by=user.id,
            )
            session.add(document)
            await session.flush()

            profile = CandidateProfile(
                candidate_id=candidate.id,
                document_id=document.id,
                version_no=1,
                status=CandidateProfileStatus.READY,
                profile_json={
                    "name": payload.name,
                    "summary": payload.summary,
                    "skills": payload.skills,
                    "years_experience": payload.years,
                    "education": payload.education,
                },
                normalized_skills=list(payload.skills),
                years_experience=payload.years,
                education_level=payload.education,
                confirmed_by=user.id,
                confirmed_at=datetime.now(UTC),
            )
            session.add(profile)
            await session.flush()

            for index, (section, text) in enumerate(payload.chunks):
                chunk = EvidenceChunk(
                    document_id=document.id,
                    candidate_profile_id=profile.id,
                    chunk_index=index,
                    section_type=section,
                    locator_json={"page": index + 1, "bbox": [0, 0, 100, 20]},
                    text=text,
                    text_sha256=_sha256(text),
                    embedding=make_embedding(text),
                    embedding_model="synthetic-seed-v1",
                    embedding_version="v1",
                )
                session.add(chunk)

            created += 1

        if created:
            print(f"[seed] created {created} candidate profiles with evidence chunks")
        else:
            print("[seed] all demo candidates already present")

        # 4) Durable hand-off records from MatchRun ranking to ApplicationRun.
        demo_candidate_ids = list(
            (
                await session.execute(
                    select(Candidate.id).where(
                        Candidate.display_name.like(f"{DEMO_CANDIDATE_PREFIX}%")
                    )
                )
            ).scalars()
        )
        existing_application_candidate_ids = set(
            (
                await session.execute(
                    select(JobApplication.candidate_id).where(
                        JobApplication.job_id == job.id,
                        JobApplication.candidate_id.in_(demo_candidate_ids),
                    )
                )
            ).scalars()
        )
        missing_application_candidate_ids = [
            candidate_id
            for candidate_id in demo_candidate_ids
            if candidate_id not in existing_application_candidate_ids
        ]
        session.add_all(
            [
                JobApplication(
                    job_id=job.id,
                    candidate_id=candidate_id,
                    status=ApplicationStatus.CREATED,
                    version=1,
                )
                for candidate_id in missing_application_candidate_ids
            ]
        )
        print(
            "[seed] ensured "
            f"{len(demo_candidate_ids)} demo JobApplications "
            f"({len(missing_application_candidate_ids)} created)"
        )

        await uow.commit()

    await engine.dispose()
    print("[seed] done.")
    print(f"[seed] login as username='{DEMO_USERNAME}' password='{DEMO_PASSWORD}'")
    print(
        f"[seed] hiring manager username='{DEMO_MANAGER_USERNAME}' "
        f"password='{DEMO_PASSWORD}' (assigned to the demo job)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed resume-copilot demo data")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing demo rows before seeding (re-runnable).",
    )
    args = parser.parse_args()
    import asyncio

    asyncio.run(seed(reset=args.reset))


if __name__ == "__main__":
    main()
