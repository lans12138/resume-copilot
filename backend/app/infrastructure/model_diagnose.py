"""Model connectivity diagnostic (FIN-008).

Run as ``python -m backend.app.infrastructure.model_diagnose``. Probes the chat
and embedding endpoints with the smallest possible request and reports whether
each is reachable, which model answered, how long it took, and — when it failed —
whether the failure is retryable or permanent.

**This command reads ``Settings``, unlike ``evaluations.gate``.** That is a
deliberate difference, not an oversight. The gate is intentionally
configuration-free because it runs in a bare CI step; this command exists purely
to answer "is the endpoint my configuration points at actually working?", so it
must read the same configuration the application does. Making it ignore settings
would make it useless.

**It never prints credentials.** The API key is never echoed, and the base URL is
reduced to scheme + host + path so a key embedded in a query string or in the
userinfo cannot leak. That matters more here than anywhere else, because the
output of a connectivity check is exactly the thing a person pastes into a
ticket.

Exit codes mirror the gate's contract so both are usable as CI or runbook steps:

* ``0`` — every probe succeeded.
* ``1`` — a probe failed permanently (bad key, wrong URL, unusable reply).
* ``2`` — a probe failed transiently (timeout, 429, 5xx), or the run could not
  start. Distinct from ``1`` because an operator's next action differs: a
  permanent failure needs a configuration fix, a transient one usually just needs
  a retry.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Sequence

from backend.app.core.settings import Settings, get_settings
from backend.app.infrastructure.http_transport import (
    PermanentTransportError,
    RetryableTransportError,
    TransportError,
)

EXIT_OK = 0
EXIT_PERMANENT_FAILURE = 1
EXIT_TRANSIENT_FAILURE = 2

# One short token is enough to prove the endpoint answers; anything longer only
# costs latency and tokens.
_PROBE_TEXT = "connectivity probe"


def _safe_endpoint(base_url: str | None) -> str:
    """Reduce a base URL to a form that cannot carry a credential.

    Query strings and userinfo are dropped entirely rather than sanitised: a
    redaction that has to guess which parameter is secret will eventually guess
    wrong, and this value is printed.

    A URL that does not parse is replaced rather than echoed. Echoing it is the
    tempting shortcut and it is exactly backwards: an unparseable value is the
    string most likely to have a key pasted into it by hand, and it is the one
    case where no component-level redaction can be attempted because there are
    no components to redact.
    """
    if not base_url:
        return "(unset)"

    from urllib.parse import urlsplit

    try:
        parts = urlsplit(base_url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        # ``urlsplit`` itself can raise on an out-of-range port.
        return "(unparsed)"

    if not parts.scheme or not host:
        return "(unparsed)"
    suffix = f":{port}" if port else ""
    return f"{parts.scheme}://{host}{suffix}{parts.path.rstrip('/')}"


def _describe_failure(error: BaseException) -> tuple[str, str]:
    """Return ``(kind, description)`` for a probe failure, without leaking secrets."""
    if isinstance(error, RetryableTransportError):
        return "retryable", f"{type(error).__name__}: {error}"
    if isinstance(error, PermanentTransportError):
        return "permanent", f"{type(error).__name__}: {error}"
    if isinstance(error, TransportError):
        return "permanent", f"{type(error).__name__}: {error}"
    return "permanent", f"{type(error).__name__}: {error}"


async def probe_chat(settings: Settings) -> tuple[bool, str]:
    """Probe the chat endpoint. Returns ``(ok, human_line)``.

    Gateway construction sits *inside* the try block. The factory raises when the
    endpoint or key is missing, and a half-configured environment is one of the
    most common reasons to be running this command at all — a raw traceback would
    bury that diagnosis behind a stack trace.
    """
    from backend.app.infrastructure.qwen_chat import build_qwen_chat_gateway

    started = time.perf_counter()
    try:
        gateway = build_qwen_chat_gateway(settings)
        # A deliberately minimal extraction: the goal is to prove the endpoint
        # accepts a structured-output request, not to judge extraction quality.
        await gateway.extract_profile(full_text=_PROBE_TEXT, blocks=[])
    except Exception as error:  # noqa: BLE001 - diagnostic boundary
        kind, description = _describe_failure(error)
        return False, f"chat        FAILED ({kind}) in {_elapsed(started)} — {description}"
    return True, (
        f"chat        OK   in {_elapsed(started)} — model={gateway.model} "
        f"version={gateway.version}"
    )


async def probe_embedding(settings: Settings) -> tuple[bool, str]:
    """Probe the embedding endpoint. Returns ``(ok, human_line)``."""
    from backend.app.infrastructure.qwen_embedding import build_qwen_embedding_gateway

    started = time.perf_counter()
    try:
        gateway = build_qwen_embedding_gateway(settings)
        vectors = await gateway.embed([_PROBE_TEXT])
    except Exception as error:  # noqa: BLE001 - diagnostic boundary
        kind, description = _describe_failure(error)
        return False, f"embedding   FAILED ({kind}) in {_elapsed(started)} — {description}"
    got = len(vectors[0]) if vectors else 0
    return True, (
        f"embedding   OK   in {_elapsed(started)} — model={gateway.model} "
        f"dimension={got} (configured {gateway.dimension})"
    )


def _elapsed(started: float) -> str:
    return f"{time.perf_counter() - started:.2f}s"


def _report_header(settings: Settings) -> str:
    mode = "FakeModel (no network)" if settings.mock_model_mode else "real Qwen"
    rule = "=" * 68
    return "\n".join(
        [
            "model connectivity diagnostic",
            rule,
            f"  mode          {mode}",
            f"  endpoint      {_safe_endpoint(settings.model_base_url)}",
            f"  chat model    {settings.chat_model}",
            f"  embed model   {settings.embedding_model} "
            f"(dimension {settings.embedding_dimension})",
            f"  timeout       {settings.model_timeout_seconds}s",
            rule,
        ]
    )


async def _run_probes(settings: Settings, *, skip_chat: bool, skip_embedding: bool) -> list[str]:
    lines: list[str] = []
    failures: list[str] = []

    if skip_chat:
        lines.append("chat        SKIPPED (--skip-chat)")
    else:
        ok, line = await probe_chat(settings)
        lines.append(line)
        if not ok:
            failures.append(line)

    if skip_embedding:
        lines.append("embedding   SKIPPED (--skip-embedding)")
    else:
        ok, line = await probe_embedding(settings)
        lines.append(line)
        if not ok:
            failures.append(line)

    if failures:
        lines.append("")
        lines.append(f"RESULT: FAILED ({len(failures)} probe(s))")
    else:
        lines.append("")
        lines.append("RESULT: OK")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """Run the diagnostic and return the process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.infrastructure.model_diagnose",
        description=(
            "Probe the configured chat and embedding endpoints. Never prints credentials. "
            "Exit 0 = both reachable, 1 = permanent failure, 2 = transient failure."
        ),
    )
    parser.add_argument("--skip-chat", action="store_true", help="Do not probe the chat endpoint.")
    parser.add_argument(
        "--skip-embedding", action="store_true", help="Do not probe the embedding endpoint."
    )
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
    except Exception as error:  # noqa: BLE001 - configuration boundary
        # Settings validation failing is itself the diagnosis: the run cannot
        # start, which is a configuration problem rather than a transient one.
        print(f"configuration invalid: {type(error).__name__}", file=sys.stderr)
        return EXIT_PERMANENT_FAILURE

    print(_report_header(settings))

    if settings.mock_model_mode:
        # Probing a fake gateway would prove nothing about the endpoint, so say so
        # rather than printing a misleading green result.
        print("")
        print("mock_model_mode is enabled: no remote endpoint is contacted.")
        print("Set MOCK_MODEL_MODE=false to probe the real adapter.")
        print("")
        print("RESULT: OK (mock mode, nothing to probe)")
        return EXIT_OK

    try:
        lines = asyncio.run(
            _run_probes(settings, skip_chat=args.skip_chat, skip_embedding=args.skip_embedding)
        )
    except Exception as error:  # noqa: BLE001 - diagnostic boundary
        print(f"diagnostic could not run: {type(error).__name__}", file=sys.stderr)
        return EXIT_TRANSIENT_FAILURE

    for line in lines:
        print(line)

    if "RESULT: FAILED" not in "\n".join(lines):
        return EXIT_OK
    # A failure that the transport already classified as permanent keeps exit 1;
    # anything else is reported as transient so the operator retries rather than
    # mis-fixing a working configuration.
    joined = "\n".join(lines)
    return EXIT_PERMANENT_FAILURE if "(permanent)" in joined else EXIT_TRANSIENT_FAILURE


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(main())


__all__ = [
    "EXIT_OK",
    "EXIT_PERMANENT_FAILURE",
    "EXIT_TRANSIENT_FAILURE",
    "main",
    "probe_chat",
    "probe_embedding",
]
