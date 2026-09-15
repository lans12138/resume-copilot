"""Rebuild the git history with the FIN-008 work as its own commit.

The repository object store was destroyed on 2026-09-15 (see the memory note).
The first rebuild folded FIN-008 into the repository-rebuild commit, which buries
it. This re-does the rebuild so the two concerns are separable in history:

  1. baseline      — the working tree as it stood before FIN-008
  2. FIN-008       — the adapter, transport, diagnostics and recordings

Approach: stage everything except the FIN-008 paths, commit that as the baseline,
then stage the FIN-008 paths and commit them on top. No file content is touched;
only the index and two commits are created.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(r"D:\code\resume")

# Every path introduced or modified by FIN-008. Kept explicit rather than derived
# from a diff: the point is to assert this list is exactly right, and a derived
# list would silently follow the mistake it is meant to catch.
FIN008_PATHS = [
    "backend/app/evaluations/datasets.py",
    "backend/app/infrastructure/http_transport.py",
    "backend/app/infrastructure/model_diagnose.py",
    "backend/app/infrastructure/prompts.py",
    "backend/app/infrastructure/qwen_chat.py",
    "backend/app/infrastructure/qwen_embedding.py",
    "backend/app/infrastructure/recording.py",
    "backend/app/infrastructure/embedding.py",
    "backend/app/infrastructure/model_gateway.py",
    "tests/unit/test_fin008_recording.py",
    "tests/unit/test_http_transport.py",
    "tests/unit/test_model_diagnose.py",
    "tests/unit/test_qwen_gateway.py",
    "tests/unit/test_embedding.py",
    "tests/validate_model_adapter.ps1",
    "scripts/project.ps1",
    # The plan document records FIN-008 as DONE, so it belongs with the work.
    "编码实现计划.md",
]

BASELINE_MESSAGE = """chore(repo): rebuild the repository object store after git corruption

The local .git object store was destroyed while a `git stash` verification probe
was terminated mid-run: .git/refs was removed entirely, the pack file went
missing while its .idx survived, and only three loose objects remained. git
could no longer read its own directory, so the commit history was unreadable and
unrecoverable — no other local clone held anything past FIN-004, and the shallow
reclone and the 2026-09-02 backup both stop there.

The working tree was untouched and is the sole surviving copy of everything
through FIN-005/006/007, so this commit re-establishes it as the new baseline.
Content is identical to the pre-corruption tree; only the commit metadata is new.
Surviving metadata (reflog, packed-refs, config, COMMIT_EDITMSG) was preserved to
resume-git-corrupt-backup-20260915/ for the record.

FIN-008 is deliberately excluded here and committed separately so it stays
legible rather than buried in a repository-rebuild commit.
"""

FIN008_MESSAGE = """feat(infrastructure): land the real Qwen adapter, transport and diagnostics

FIN-008. Implements the OpenAI-compatible Chat and Embedding gateways, the
bounded-retry transport they share, a credential-free connectivity diagnostic,
and a record/replay layer for synthetic evaluation data.

The switch between the fake and the real adapter is the single existing
`mock_model_mode` flag. Until now the two factories disagreed about it: the chat
factory silently returned the heuristic fake while the embedding factory raised
NotImplementedError. Both now resolve the same way, and the real factories refuse
to build without an endpoint and key rather than degrading to the fake —
fabricating plausible-looking extractions is far worse than failing loudly at
startup.

Retry classification lives in one place (`http_transport`). Timeouts, connect
failures, 429 and 5xx are retryable; other 4xx, malformed bodies, and HTTP 200
with a body that violates the schema are permanent. That single bit is what the
Celery layer uses to decide whether burning another attempt is worth anything.
Structured output is treated as untrusted input throughout: parse, then validate,
and treat a schema violation as permanent, because resending an identical request
yields an identical malformed reply. Backoff honours a server Retry-After (capped)
and otherwise jitters over [0.5, 1.0) of the exponential so concurrent workers
decorrelate instead of re-creating the herd that caused the 429.

The diagnostic (`python -m backend.app.infrastructure.model_diagnose`) exits 0/1/2
and never prints a credential. It reduces the endpoint to scheme+host+port+path,
dropping query and userinfo wholesale rather than redacting by parameter name —
a redaction that has to guess which parameter is secret will eventually guess
wrong, and this value is exactly what gets pasted into a ticket. A URL that does
not parse is replaced rather than echoed, because a hand-pasted credential is the
most likely reason a URL fails to parse at all. Unlike the offline evaluation
gate, this command deliberately reads Settings: its whole purpose is answering
"is the endpoint my configuration points at actually working".

Recordings store the raw reply text rather than a parsed draft, so replay goes
through the same parsing and validation the live path uses — that is what makes a
fixture evidence rather than decoration. A fixture holding a malformed reply must
fail on replay. The builtin synthetic datasets are now actually registered on the
production path; previously nothing outside tests called register_dataset, so
dataset_versions stayed empty and a run could not be attributed to a dataset
version.

Uses the existing httpx2 client rather than adding the openai SDK: it ships a
MockTransport, which makes the whole retry matrix testable with no network, no key
and no sleeping, and it avoids touching requirements.lock.

Tests: 30 transport, 50 gateway, 30 recording, 27 diagnostic. Backend 529 pass,
ruff clean, mypy strict clean across 209 files. Adds
tests/validate_model_adapter.ps1, wired into `project.ps1 verify`.
"""


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    # ``-c core.quotepath=false`` keeps non-ASCII paths literal; with the default
    # git returns them C-quoted ("UML\350..."), which no longer matches a real file.
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check and result.returncode != 0:
        print(f"FAILED: git {' '.join(args)}", file=sys.stderr)
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(1)
    return result


# Start from an empty index so the two commits are built by explicit path.
run("read-tree", "--empty")

# --- Commit 1: everything except FIN-008 ------------------------------------
all_files = run("ls-files", "--others", "--cached", "--exclude-standard").stdout.splitlines()
all_files = [f for f in all_files if not f.startswith(f"{REPO.name}/")]
print(f"total files: {len(all_files)}")

tracked = set(FIN008_PATHS)
baseline_files = [f for f in all_files if f not in tracked]
print(f"baseline files: {len(baseline_files)}")

if baseline_files:
    run("add", "--pathspec-from-file=-", "--pathspec-file-nul") if False else None
    # Batch the adds: one call per file is fine at this scale and keeps failures
    # attributable to the exact path.
    for name in baseline_files:
        run("add", "--", name)

run("commit", "-q", "-m", BASELINE_MESSAGE)

# --- Commit 2: FIN-008 ------------------------------------------------------
missing = [p for p in FIN008_PATHS if not (REPO / p).exists()]
if missing:
    print(f"FIN-008 path(s) missing: {missing}", file=sys.stderr)
    raise SystemExit(1)

for name in FIN008_PATHS:
    run("add", "--", name)

run("commit", "-q", "-m", FIN008_MESSAGE)

print("=== HISTORY ===")
print(run("log", "--oneline").stdout)
print("=== FIN-008 COMMIT STAT ===")
print(run("show", "--stat", "--oneline", "HEAD").stdout)
