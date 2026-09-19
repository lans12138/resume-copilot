"""Which commit and which versions produced these numbers (PORT-006).

A metrics report is only usable if a reader can go from a figure back to the code
that produced it. The report already carried the *data* versions — dataset content
hash, prompt version, rule version, gateway and embedding identifiers — but not the
repository revision, so "these are the numbers for the release candidate" was an
assertion nobody could check.

Two things this module refuses to do:

* **Invent a revision.** When git cannot be read — no repository, no git binary, a
  timeout — the report says 未记录 rather than dropping the line. An absent
  provenance line reads as "not applicable"; the truth is "we could not check", and
  those are different statements to put in front of a reader.
* **Claim a clean tree it did not see.** A report generated from uncommitted edits
  cannot be attributed to the commit alone. ``dirty`` is therefore three-valued:
  ``True``, ``False``, or ``None`` for "could not tell". Rendering collapses
  ``True`` and ``None`` into two different warnings on purpose.

Reading git must never fail the evaluation. The evaluation's job is to measure; a
missing git binary is not a measurement failure, and turning it into one would make
the gate depend on the environment it is describing.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

#: How long a git probe may take before it is treated as unavailable.
PROBE_TIMEOUT_SECONDS = 5

#: What the report prints when a value could not be read.
UNRECORDED = "未记录"


@dataclass(frozen=True, slots=True)
class Provenance:
    """The revision and invocation a report came from.

    ``commit`` / ``dirty`` / ``generated_at`` are ``None`` when unreadable; the
    renderer turns that into an explicit 未记录 line instead of an omission.
    """

    command: str
    commit: str | None = None
    dirty: bool | None = None
    generated_at: datetime | None = None

    @property
    def short_commit(self) -> str | None:
        """The 8-character form a reader can paste into ``git show``."""
        return None if self.commit is None else self.commit[:8]

    @property
    def revision_note(self) -> str | None:
        """How much the revision can be trusted, or ``None`` when it is exact.

        Three-valued on purpose: a report generated from uncommitted edits cannot
        be attributed to the commit alone, and "we could not tell" is a third
        statement again. Collapsing either into "clean" would overstate the report.
        """
        if self.commit is None:
            return "无法读取 git 状态，本报告不能归属到某个提交"
        if self.dirty is True:
            return "工作区有未提交改动，本报告不完全对应此提交"
        if self.dirty is None:
            return "无法确认工作区是否干净"
        return None

    @property
    def generated_label(self) -> str:
        if self.generated_at is None:
            return UNRECORDED
        return self.generated_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(args: Sequence[str], cwd: str | None) -> str | None:
    """Run one git command, returning its stdout or ``None`` if it cannot run."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        # No git binary, no repository, or a probe that hung: all three mean the
        # same thing to a reader, so they collapse into one outcome.
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def read_revision(
    cwd: str | None = None,
    *,
    run: Callable[[Sequence[str], str | None], str | None] | None = None,
) -> tuple[str | None, bool | None]:
    """Return ``(commit_sha, dirty)``, either of which may be ``None``.

    ``run`` is the seam tests use; production callers leave it out.
    """
    probe = _git if run is None else run
    commit = probe(["rev-parse", "HEAD"], cwd)
    if commit is None:
        return None, None
    status = probe(["status", "--porcelain"], cwd)
    # A clean tree produces no output, which is indistinguishable from "the probe
    # failed" once the string is empty — so an unreadable status is reported as
    # unknown rather than as clean.
    dirty = None if status is None else bool(status.strip())
    return commit.strip(), dirty


def describe_provenance(
    command: str,
    *,
    cwd: str | None = None,
    now: datetime | None = None,
    run: Callable[[Sequence[str], str | None], str | None] | None = None,
) -> Provenance:
    """Build the provenance block for a report produced by ``command``."""
    commit, dirty = read_revision(cwd, run=run)
    return Provenance(
        command=command,
        commit=commit,
        dirty=dirty,
        generated_at=datetime.now(UTC) if now is None else now,
    )


def render_provenance(provenance: Provenance) -> list[str]:
    """Render the provenance as Markdown lines, placed under the report title."""
    revision = provenance.short_commit or UNRECORDED
    note = provenance.revision_note
    suffix = "" if note is None else f"（{note}）"
    return [
        "## 溯源",
        "",
        f"- 代码版本：`{revision}`{suffix}",
        f"- 生成时间：`{provenance.generated_label}`（UTC）",
        f"- 生成命令：`{provenance.command}`",
        "",
        "报告只描述上面这一次运行：换了提交、换了数据集版本或换了网关，数字都要重新测，"
        "不能与旧报告并列比较。",
        "",
    ]
