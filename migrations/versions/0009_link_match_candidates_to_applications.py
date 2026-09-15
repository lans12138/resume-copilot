"""Link MatchRun candidate snapshots to real JobApplications.

Revision ID: 0009_link_applications
Revises: 0008_create_workflow_tables
"""

from __future__ import annotations

from alembic import op

revision = "0009_link_applications"
down_revision = "0008_create_workflow_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Older MatchRuns stored candidate_profile_id as a placeholder application id.
    # Create/reuse the canonical aggregate first, then repoint every snapshot before
    # adding the FK. Existing application status is never overwritten.
    op.execute(
        """
        INSERT INTO job_applications (id, job_id, candidate_id, status, version)
        SELECT gen_random_uuid(), mr.job_id, cp.candidate_id, 'CREATED', 1
        FROM match_run_candidates AS mrc
        JOIN match_runs AS mr ON mr.run_id = mrc.run_id
        JOIN candidate_profiles AS cp ON cp.id = mrc.candidate_profile_id
        ON CONFLICT (job_id, candidate_id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE match_run_candidates AS mrc
        SET application_id = ja.id
        FROM match_runs AS mr, candidate_profiles AS cp, job_applications AS ja
        WHERE mr.run_id = mrc.run_id
          AND cp.id = mrc.candidate_profile_id
          AND ja.job_id = mr.job_id
          AND ja.candidate_id = cp.candidate_id
        """
    )
    op.create_foreign_key(
        "fk_match_run_candidates_application",
        "match_run_candidates",
        "job_applications",
        ["application_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_match_run_candidates_application",
        "match_run_candidates",
        type_="foreignkey",
    )
