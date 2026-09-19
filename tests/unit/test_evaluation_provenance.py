"""The report names the revision it measured, or says it could not (PORT-006).

The roadmap's acceptance asks that a model-effect report be traceable to a release
candidate commit and an evaluation version. The data versions were already in the
report; the repository revision was not, so "these numbers are for the release
candidate" was an assertion a reader could not check.

What these tests pin is mostly about the *failure* paths, because they are the ones
that could quietly overstate a report:

* a report generated from uncommitted edits must not read as a clean-commit report,
* an unreadable git status must not be reported as a clean one, and
* an unreadable revision must produce a visible 未记录 line rather than no line.

An omitted provenance line reads as "not applicable". The truth in that third case
is "we could not check", and a reader deserves the difference.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import pytest

from backend.app.evaluations.provenance import (
    UNRECORDED,
    Provenance,
    describe_provenance,
    read_revision,
    render_provenance,
)
from backend.app.evaluations.report import Section, build_report, render
from backend.app.evaluations.runner import PredictionSource

SHA = "3a261c8f1e2d3c4b5a69788796a5b4c3d2e1f001"
NOW = datetime(2026, 9, 19, 10, 30, tzinfo=UTC)


def _probe(
    *, commit: str | None = SHA, status: str | None = ""
) -> Callable[[Sequence[str], str | None], str | None]:
    """A git seam. ``None`` means the command could not be run."""

    def run(args: Sequence[str], cwd: str | None) -> str | None:
        del cwd
        if args[0] == "rev-parse":
            return None if commit is None else f"{commit}\n"
        return status

    return run


class TestReadRevision:
    def test_reports_a_clean_tree_as_clean(self) -> None:
        commit, dirty = read_revision(run=_probe())

        assert commit == SHA
        assert dirty is False

    def test_sees_uncommitted_changes(self) -> None:
        _, dirty = read_revision(run=_probe(status=" M README.md\n"))

        assert dirty is True

    def test_a_missing_commit_makes_the_whole_revision_unknown(self) -> None:
        commit, dirty = read_revision(run=_probe(commit=None))

        assert commit is None
        assert dirty is None

    def test_an_unreadable_status_is_not_reported_as_clean(self) -> None:
        # An empty string and a failed probe both look like "no output"; treating
        # the failure as clean would let a dirty tree be published as a commit.
        commit, dirty = read_revision(run=_probe(status=None))

        assert commit == SHA
        assert dirty is None

    def test_a_missing_git_binary_is_unknown_rather_than_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The real probe, not the seam: the seam's contract is already "return None
        # on failure", so the degradation being tested here is ``_git``'s.
        def explode(*args: object, **kwargs: object) -> object:
            raise FileNotFoundError("git")

        monkeypatch.setattr(subprocess, "run", explode)

        commit, dirty = read_revision()

        assert commit is None
        assert dirty is None

    def test_a_hung_probe_is_treated_as_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def hang(*args: object, **kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr(subprocess, "run", hang)

        assert read_revision() == (None, None)

    def test_a_failing_git_command_is_treated_as_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=["git"], returncode=128, stdout="")

        monkeypatch.setattr(subprocess, "run", fail)

        assert read_revision() == (None, None)


class TestDescribeProvenance:
    def test_records_the_invocation_and_the_moment(self) -> None:
        provenance = describe_provenance(
            "python scripts/run_evaluation.py --k 5", now=NOW, run=_probe()
        )

        assert provenance.command == "python scripts/run_evaluation.py --k 5"
        assert provenance.commit == SHA
        assert provenance.generated_at == NOW

    def test_the_short_form_is_pasteable_into_git_show(self) -> None:
        assert Provenance(command="x", commit=SHA).short_commit == "3a261c8f"


class TestRenderProvenance:
    def test_a_clean_report_claims_no_caveat(self) -> None:
        text = "\n".join(
            render_provenance(Provenance(command="cmd", commit=SHA, dirty=False, generated_at=NOW))
        )

        assert "`3a261c8f`" in text
        assert "未提交" not in text
        assert "无法" not in text

    def test_a_dirty_report_says_it_does_not_fully_correspond(self) -> None:
        text = "\n".join(
            render_provenance(Provenance(command="cmd", commit=SHA, dirty=True, generated_at=NOW))
        )

        assert "工作区有未提交改动，本报告不完全对应此提交" in text

    def test_an_unconfirmed_working_tree_is_not_called_clean(self) -> None:
        text = "\n".join(
            render_provenance(Provenance(command="cmd", commit=SHA, dirty=None, generated_at=NOW))
        )

        assert "无法确认工作区是否干净" in text

    def test_an_unreadable_revision_still_prints_the_line(self) -> None:
        text = "\n".join(
            render_provenance(
                Provenance(command="cmd", commit=None, dirty=None, generated_at=NOW)
            )
        )

        # The line is present and says 未记录: dropping it would read as
        # "provenance not applicable" rather than "we could not check".
        assert f"代码版本：`{UNRECORDED}`" in text
        assert "本报告不能归属到某个提交" in text

    def test_an_unknown_time_is_recorded_as_unrecorded(self) -> None:
        text = "\n".join(
            render_provenance(Provenance(command="cmd", commit=SHA, dirty=False))
        )

        assert f"生成时间：`{UNRECORDED}`" in text

    def test_the_command_is_quoted_so_it_can_be_run_again(self) -> None:
        text = "\n".join(
            render_provenance(
                Provenance(
                    command="python scripts/run_evaluation.py --k 5 --split holdout",
                    commit=SHA,
                    dirty=False,
                    generated_at=NOW,
                )
            )
        )

        assert "`python scripts/run_evaluation.py --k 5 --split holdout`" in text


class TestReportIntegration:
    def test_provenance_sits_between_the_title_and_the_numbers(self) -> None:
        section = Section(
            source=PredictionSource.FAKE_MODEL, heading="x@v1｜HOLDOUT", caveat="caveat"
        )
        report = build_report(
            "resume-copilot 离线评测报告",
            [section],
            Provenance(command="cmd", commit=SHA, dirty=False, generated_at=NOW),
        )

        text = render(report)

        assert text.index("## 溯源") < text.index("## 假模型")
        assert text.startswith("# resume-copilot 离线评测报告\n")

    def test_a_report_without_provenance_renders_without_the_block(self) -> None:
        section = Section(
            source=PredictionSource.FAKE_MODEL, heading="x@v1｜HOLDOUT", caveat="caveat"
        )

        text = render(build_report("t", [section]))

        assert "## 溯源" not in text
        assert text.startswith("# t\n")
