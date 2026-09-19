"""Display projections for ranking rows and report headings (PORT-005).

Two things are pinned here and they are different kinds of claim.

The first is a *projection* claim, checked against real ``CandidateProfile``
objects: the summary keeps a null ``years_experience`` null instead of collapsing
it to zero, converts the ``Numeric`` column to a float, and copies the skill list
rather than aliasing the ORM attribute.

The second is a *contract* claim, checked through the generated OpenAPI document:
the ranking row and the report carry the display fields at all. Both used to
identify a candidate by a truncated profile UUID, and the presenter had to hold
that string in their head to connect a row to its report and its approval.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from backend.app.candidates.models import CandidateProfile
from backend.app.candidates.summaries import (
    load_display_summaries,
    summarise_profiles,
)
from backend.app.main import create_app
from tests.unit.settings_factory import make_settings


def _profile(
    *,
    candidate_id: UUID | None = None,
    document_id: UUID | None = None,
    skills: list[str] | None = None,
    years: Decimal | None = None,
    education: str | None = None,
) -> CandidateProfile:
    now = datetime.now(UTC)
    return CandidateProfile(
        id=uuid4(),
        candidate_id=candidate_id or uuid4(),
        document_id=document_id or uuid4(),
        version_no=1,
        profile_json={"full_name": "测试候选人"},
        normalized_skills=skills if skills is not None else [],
        years_experience=years,
        education_level=education,
        schema_version="v1",
        version=1,
        created_at=now,
        updated_at=now,
    )


class _ExplodingSession:
    """A session that fails loudly, to prove an empty id set never queries."""

    async def execute(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("an empty profile id set must not reach the database")


class TestSummariseProfiles:
    def test_keys_the_summary_by_profile_id(self) -> None:
        profile = _profile()
        summaries = summarise_profiles([(profile, "张三")])

        assert list(summaries) == [profile.id]
        assert summaries[profile.id].display_name == "张三"

    def test_carries_the_source_document(self) -> None:
        """The report panel deep-links to the original text, so it needs the document."""
        document_id = uuid4()
        summary = summarise_profiles([(_profile(document_id=document_id), "张三")])
        (only,) = summary.values()

        assert only.document_id == document_id

    def test_converts_the_numeric_column_to_a_float(self) -> None:
        # ``years_experience`` is Numeric(5, 2); a Decimal would survive into the
        # JSON response as a string and the summary line would read "8.50 年经验".
        summary = summarise_profiles([(_profile(years=Decimal("8.50")), "张三")])
        (only,) = summary.values()

        assert only.years_experience == 8.5
        assert isinstance(only.years_experience, float)

    def test_keeps_a_missing_experience_count_missing(self) -> None:
        """``null`` is the absence of a fact; ``0.0`` would be a different claim."""
        summary = summarise_profiles([(_profile(years=None), "张三")])
        (only,) = summary.values()

        assert only.years_experience is None

    def test_copies_the_skill_list_instead_of_aliasing_it(self) -> None:
        profile = _profile(skills=["python"])
        summary = summarise_profiles([(profile, "张三")])
        (only,) = summary.values()

        assert only.normalized_skills == ["python"]
        assert only.normalized_skills is not profile.normalized_skills

    def test_carries_the_candidate_and_education(self) -> None:
        candidate_id = uuid4()
        summary = summarise_profiles(
            [(_profile(candidate_id=candidate_id, education="MASTER"), "张三")]
        )
        (only,) = summary.values()

        assert only.candidate_id == candidate_id
        assert only.education_level == "MASTER"

    def test_an_empty_row_set_is_an_empty_mapping(self) -> None:
        assert summarise_profiles([]) == {}

    def test_the_last_row_wins_for_a_repeated_profile(self) -> None:
        """Defensive: a caller that hands over duplicates gets one entry, not two."""
        profile = _profile()
        summaries = summarise_profiles([(profile, "张三"), (profile, "李四")])

        assert len(summaries) == 1
        assert summaries[profile.id].display_name == "李四"


class TestLoadDisplaySummaries:
    def test_an_empty_id_set_does_not_query(self) -> None:
        """``IN ()`` is not valid SQL, and a run with no candidates must not care."""
        session: Any = _ExplodingSession()

        assert asyncio.run(load_display_summaries(session, [])) == {}


class TestWireContract:
    """The display fields have to exist on the wire, or the pages cannot use them."""

    @pytest.fixture(scope="class")
    def schemas(self) -> dict[str, Any]:
        app = create_app(make_settings())
        return cast(dict[str, Any], app.openapi()["components"]["schemas"])

    def test_ranking_rows_carry_a_name_and_a_summary(
        self, schemas: dict[str, Any]
    ) -> None:
        properties = schemas["MatchRunCandidateOut"]["properties"]

        assert "display_name" in properties
        assert "normalized_skills" in properties
        assert "years_experience" in properties
        assert "education_level" in properties
        assert "document_id" in properties

    def test_reports_carry_a_name_and_the_source_document(
        self, schemas: dict[str, Any]
    ) -> None:
        properties = schemas["ReportOut"]["properties"]

        assert "display_name" in properties
        assert "document_id" in properties

    def test_a_ranking_row_still_validates_without_its_display_fields(
        self, schemas: dict[str, Any]
    ) -> None:
        """The ranking is the durable record; the name is a courtesy.

        A row whose profile can no longer be read must still be serialisable, or a
        missing name would turn a reportable ranking into a 500.
        """
        from backend.app.match_run.schemas import MatchRunCandidateOut

        row = MatchRunCandidateOut.model_validate(
            {
                "candidate_profile_id": uuid4(),
                "application_id": uuid4(),
                "snapshot_order": 1,
                "rrf_score": 0.5,
                "processing_status": "COMPLETED",
                "hard_rule_overall": "PASS",
            }
        )

        assert row.display_name is None
        assert row.normalized_skills == []
        assert row.document_id is None

    def test_the_profile_id_is_still_exposed(self, schemas: dict[str, Any]) -> None:
        """Demoted to a tracking detail, never removed: support and the API use it."""
        required = schemas["MatchRunCandidateOut"]["required"]

        assert "candidate_profile_id" in required
        assert "display_name" not in required
