"""Central SQLAlchemy model registry used by Alembic and schema tests.

Every mapped model must be imported here.  Keeping this list explicit makes a
new table fail fast in tests instead of silently disappearing from Alembic's
``target_metadata``.
"""

from __future__ import annotations

from sqlalchemy import MetaData

from backend.app.agent import models as agent_models
from backend.app.approvals import models as approval_models
from backend.app.auth import models as auth_models
from backend.app.candidates import models as candidate_models
from backend.app.documents import models as document_models
from backend.app.infrastructure.database import Base
from backend.app.interviews import models as interview_models
from backend.app.job_applications import models as application_models
from backend.app.jobs import models as job_models
from backend.app.match_run import models as match_run_models
from backend.app.reports import models as report_models

MIGRATED_MODEL_TABLES = frozenset(
    {
        "agent_events",
        "agent_checkpoints",
        "agent_runs",
        "application_runs",
        "application_status_history",
        "approvals",
        "candidate_profiles",
        "candidates",
        "claim_evidences",
        "evidence_chunks",
        "interviews",
        "job_applications",
        "job_assignments",
        "job_versions",
        "jobs",
        "match_reports",
        "match_run_candidates",
        "match_runs",
        "report_claims",
        "resume_documents",
        "users",
    }
)

_LOADED_MODEL_MODULES = (
    agent_models,
    approval_models,
    auth_models,
    candidate_models,
    document_models,
    interview_models,
    application_models,
    job_models,
    match_run_models,
    report_models,
)


def get_model_metadata() -> MetaData:
    """Return complete metadata, failing if the explicit registry drifts."""
    # Referencing the tuple makes the side-effect imports explicit: importing
    # each module above registers its mapped tables on ``Base.metadata``.
    if not _LOADED_MODEL_MODULES:
        raise RuntimeError("no SQLAlchemy model modules were loaded")
    metadata_tables = frozenset(Base.metadata.tables)
    if metadata_tables != MIGRATED_MODEL_TABLES:
        missing = sorted(MIGRATED_MODEL_TABLES - metadata_tables)
        unexpected = sorted(metadata_tables - MIGRATED_MODEL_TABLES)
        raise RuntimeError(
            f"SQLAlchemy metadata drift: missing={missing}, unexpected={unexpected}"
        )
    return Base.metadata
