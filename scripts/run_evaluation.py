"""Run the offline evaluation and print one report per prediction source (PORT-004).

This is the explicit trigger the roadmap asks for. Nothing runs on import and
nothing runs on a schedule: an evaluation that reports numbers must be started by
someone who then reads them.

It also does the one thing the old pipeline could not — it *fails* instead of
concluding. A missing recording, an empty corpus or a run that measured no case
exits non-zero with a reason, because a gate that cannot measure must not be able
to say "passed".

Usage::

    # CI default: deterministic stand-in, no network, no key
    python scripts/run_evaluation.py --k 5 --split holdout

    # replay a checked-in recording through the real parsing path
    python scripts/run_evaluation.py --recording tests/fixtures/recordings/extraction.json

    # live model; requires a real endpoint and key in the environment
    python scripts/run_evaluation.py --live

The report always keeps the built-in scorer suites in their own section. They are
not comparable with a corpus run and are never averaged into it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Make ``backend`` importable whether run from repo root or /workspace.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.core.settings import get_settings  # noqa: E402
from backend.app.evaluations.budget import BudgetedGateway  # noqa: E402
from backend.app.evaluations.corpus import BUILTIN_CORPUS, Split  # noqa: E402
from backend.app.evaluations.executor import BuiltinEvaluationExecutor  # noqa: E402
from backend.app.evaluations.injection import (  # noqa: E402
    InjectionRunReport,
    observe_dataset,
    summarise,
)
from backend.app.evaluations.injection_corpus import (  # noqa: E402
    BUILTIN_INJECTION_DATASET,
)
from backend.app.evaluations.metrics import CorpusMetrics, evaluate  # noqa: E402
from backend.app.evaluations.report import (  # noqa: E402
    SOURCE_CAVEATS,
    FixtureMetric,
    Pricing,
    Section,
    build_report,
    render,
)
from backend.app.evaluations.runner import (  # noqa: E402
    EvaluationSetupError,
    PredictionSource,
    run_corpus,
    underlying_gateway,
)
from backend.app.infrastructure.embedding import build_embedding_gateway  # noqa: E402
from backend.app.infrastructure.model_gateway import ModelGateway  # noqa: E402
from backend.app.infrastructure.recording import (  # noqa: E402
    RecordingError,
    build_gateway_for_mode,
)
from backend.app.retrieval.models import RetrievalConfig  # noqa: E402

EXIT_OK = 0
#: The run produced no number to be right or wrong about.
EXIT_CANNOT_CONCLUDE = 1
#: The run could not start: missing recording, empty corpus, bad configuration.
EXIT_SETUP_FAILED = 2
#: A measured run that broke a zero bar (an attack the system permitted).
EXIT_GATE_FAILED = 3

_SPLITS: dict[str, Split | None] = {"dev": Split.DEV, "holdout": Split.HOLDOUT, "all": None}

#: ``Settings`` requires ``storage_root`` to be absolute, and the repo's ``.env``
#: points it at ``/data/resumes`` — absolute inside the container, but not on
#: Windows, which needs a drive letter. The evaluation reads no file storage at
#: all, so rather than making a local run fail on an unrelated field the script
#: substitutes a repo-local absolute path when the configured one is unusable for
#: this platform. An explicit environment value always wins.
_ENV_STORAGE_ROOT = "STORAGE_ROOT"
_DEFAULT_STORAGE_ROOT = "/data/resumes"


def _ensure_storage_root_is_absolute() -> None:
    configured = Path(os.environ.get(_ENV_STORAGE_ROOT, _DEFAULT_STORAGE_ROOT))
    if configured.is_absolute():
        return
    repo_root = Path(__file__).resolve().parent.parent
    os.environ[_ENV_STORAGE_ROOT] = str(repo_root / "storage")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline evaluation report (PORT-004)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--recording",
        type=Path,
        default=None,
        help="replay a checked-in recording instead of calling a model",
    )
    source.add_argument(
        "--live",
        action="store_true",
        help="call the real model; requires MODEL_BASE_URL and QWEN_API_KEY",
    )
    parser.add_argument("--k", type=int, default=5, help="ranking cut-off (default 5)")
    parser.add_argument(
        "--split",
        choices=sorted(_SPLITS),
        default="holdout",
        help="which half to report; the published number uses holdout (default)",
    )
    parser.add_argument("--output", type=Path, default=None, help="write the report here too")
    parser.add_argument(
        "--no-fixtures",
        action="store_true",
        help="omit the built-in scorer section (it is a different source)",
    )
    parser.add_argument(
        "--no-injection",
        action="store_true",
        help="omit the clean/injected pair observation (also a different measurement)",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=None,
        help="stop the run after this many extraction calls (a live run must state one)",
    )
    parser.add_argument(
        "--allow-unbudgeted",
        action="store_true",
        help="permit a live run with no call budget; the absence is recorded in the report",
    )
    parser.add_argument(
        "--prompt-price",
        type=float,
        default=None,
        help="price per 1k prompt tokens; requires --price-version",
    )
    parser.add_argument(
        "--completion-price", type=float, default=None, help="price per 1k completion tokens"
    )
    parser.add_argument(
        "--price-version",
        default=None,
        help="the recorded price list these rates came from; without it no cost is printed",
    )
    return parser.parse_args(argv)


def _pricing(args: argparse.Namespace) -> Pricing | None:
    """Build a price list only when every part of it is stated.

    A rate without a version cannot be checked or updated, so an incomplete price
    list yields no estimate rather than a number nobody can audit.
    """
    if args.prompt_price is None or args.completion_price is None or not args.price_version:
        return None
    return Pricing(
        version=args.price_version,
        currency="CNY",
        prompt_per_1k=args.prompt_price,
        completion_per_1k=args.completion_price,
    )


def _fixture_section(config: RetrievalConfig) -> Section:
    """The built-in hermetic suites, in their own section and never merged."""
    results = BuiltinEvaluationExecutor(retrieval_config=config).run_all()
    return Section(
        source=PredictionSource.SCORER_FIXTURE,
        heading="内置套件（FIN-007 / FIN-008）",
        caveat=SOURCE_CAVEATS[PredictionSource.SCORER_FIXTURE],
        fixtures=tuple(
            FixtureMetric(
                name=item.metric_name,
                value=item.metric_value,
                threshold=item.threshold,
                passed=item.passed,
            )
            for item in results
        ),
    )


def _corpus_section(
    metrics: CorpusMetrics,
    pricing: Pricing | None,
    injection: InjectionRunReport | None,
    notes: tuple[str, ...],
) -> Section:
    scope = metrics.split.value if metrics.split else "全部"
    return Section(
        source=metrics.source,
        heading=f"{metrics.corpus_name}@{metrics.corpus_version}｜{scope}",
        caveat=SOURCE_CAVEATS[metrics.source],
        metrics=metrics,
        injection=injection,
        notes=notes,
        pricing=pricing,
    )


def _budgeted_gateway(args: argparse.Namespace, gateway: ModelGateway) -> ModelGateway:
    """Apply the stated call budget, and refuse to let a live run omit one.

    A live evaluation is the only part of this system that spends money per run, so
    the number of calls it may make is a parameter of the run rather than something
    reconstructed from the bill. Refusing an unbudgeted live run is deliberate: the
    operator who wanted no limit can say so, and the report records that they did.
    """
    if args.max_calls is None:
        if args.live and not args.allow_unbudgeted:
            raise EvaluationSetupError(
                "EVALUATION_BUDGET_NOT_STATED",
                "真实模型评测必须声明调用预算：--max-calls N"
                "（确实不限时请显式加 --allow-unbudgeted）",
            )
        return gateway
    return BudgetedGateway(gateway, max_calls=args.max_calls)


def _budget_note(args: argparse.Namespace) -> tuple[str, ...]:
    if args.max_calls is None:
        return ("本次未声明调用预算（--max-calls），实际调用次数不受评测入口限制。",)
    return (f"本次调用预算 {args.max_calls} 次；超出即停止评测，不产生结论。",)


async def _run(args: argparse.Namespace) -> int:
    _ensure_storage_root_is_absolute()
    settings = get_settings()
    if args.live:
        settings = settings.model_copy(update={"mock_model_mode": False})
    if args.k <= 0:
        raise EvaluationSetupError("EVALUATION_INVALID_K", f"--k must be positive, got {args.k}")
    gateway = _budgeted_gateway(
        args, build_gateway_for_mode(settings, recording_path=args.recording)
    )
    embedding_gateway = build_embedding_gateway(settings)
    config = RetrievalConfig.from_settings(settings)

    split = _SPLITS[args.split]

    # The run holds exactly the cases being reported on, so the ranking is built
    # over the same pool it is scored against. Ranking one pool and reporting
    # another would give a number about neither.
    cases = BUILTIN_CORPUS.cases if split is None else BUILTIN_CORPUS.split(split)
    run = await run_corpus(
        corpus=BUILTIN_CORPUS,
        cases=cases,
        gateway=gateway,
        embedding_gateway=embedding_gateway,
        config=config,
        model_name=getattr(underlying_gateway(gateway), "model", ""),
    )
    metrics = evaluate(run, BUILTIN_CORPUS, k=args.k, split=split)

    # The injection half runs over the same half of the corpus and the same gateway,
    # so it is the same source and belongs in the same section. A different gateway
    # would make it a different run, and ``build_report`` would refuse to merge them.
    injection: InjectionRunReport | None = None
    if not args.no_injection:
        injection = summarise(
            await observe_dataset(
                dataset=BUILTIN_INJECTION_DATASET,
                corpus=BUILTIN_CORPUS,
                pairs=BUILTIN_INJECTION_DATASET.split(split)
                if split is not None
                else BUILTIN_INJECTION_DATASET.pairs,
                gateway=gateway,
                embedding_gateway=embedding_gateway,
                config=config,
                model_name=getattr(underlying_gateway(gateway), "model", ""),
            )
        )

    sections = [_corpus_section(metrics, _pricing(args), injection, _budget_note(args))]
    if not args.no_fixtures:
        sections.append(_fixture_section(config))

    text = render(build_report("resume-copilot 离线评测报告", sections))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    sys.stdout.write(text)

    if not metrics.concluded:
        sys.stderr.write("\n评测未能得出结论：没有任何用例可测量，不产生通过结论。\n")
        return EXIT_CANNOT_CONCLUDE
    if injection is not None and not injection.concluded:
        sys.stderr.write("\n注入评测未能得出结论：没有任何用例可观测，不产生通过结论。\n")
        return EXIT_CANNOT_CONCLUDE
    if injection is not None and not injection.passed:
        sys.stderr.write(
            f"\n注入门槛未通过：系统放行 {injection.system_permitted_pairs} 例"
            "（越权 / 副作用 / 审批绕过），门槛为 0。\n"
        )
        return EXIT_GATE_FAILED
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return asyncio.run(_run(args))
    except EvaluationSetupError as error:
        sys.stderr.write(f"\n评测无法开始[{error.code}]：{error.safe_message}\n")
        return EXIT_SETUP_FAILED
    except RecordingError as error:
        sys.stderr.write(f"\n评测无法开始：录制文件不可用（{error}）\n")
        return EXIT_SETUP_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
