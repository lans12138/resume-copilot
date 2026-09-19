"""Render the evaluation report, keeping the three prediction sources apart.

The roadmap's requirement is that a report distinguishes 计分器测试, 录制回放 and
真实模型评测 and never mixes their results, and the reason is not tidiness. The
old suites' numbers were produced by scoring built-in predictions against
built-in labels; a replayed recording exercises the real parsing contract without
a network; a live call is the only one that measures the model. Averaging them
would produce a figure that describes no run that ever happened, and a reader
would have no way to tell.

So this module has exactly one structural rule: **one section per source, and a
source may not appear twice.** ``build_report`` raises on a duplicate rather than
merging, because the caller who wanted a merged number wants something that cannot
exist.

Every section prints its own caveat next to its numbers. A figure without its
scope is precisely the failure this work package exists to correct.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.evaluations.metrics import CorpusMetrics, Stage
from backend.app.evaluations.runner import PredictionSource

#: How each source is named in the report.
SOURCE_LABELS: dict[PredictionSource, str] = {
    PredictionSource.SCORER_FIXTURE: "计分器测试",
    PredictionSource.FAKE_MODEL: "假模型（Mock 模式）",
    PredictionSource.RECORDED_REPLAY: "录制回放",
    PredictionSource.LIVE_MODEL: "真实模型评测",
}

#: What each source does and does not establish. Printed with the numbers.
SOURCE_CAVEATS: dict[PredictionSource, str] = {
    PredictionSource.SCORER_FIXTURE: (
        "预测由内置夹具产生，标准答案同样是内置的；只证明计分器与阈值的行为未变，"
        "不能作为系统能力的证据。"
    ),
    PredictionSource.FAKE_MODEL: (
        "预测来自确定性启发式替身，未经真实模型。可以反映链路是否通、确定性环节是否正确，"
        "但抽取与语义指标受替身词表限制，不可外推到真实模型。"
    ),
    PredictionSource.RECORDED_REPLAY: (
        "预测来自真实模型的历史响应，回放走的是真实解析与校验路径；"
        "证明契约未腐化，但不反映当前模型权重下的表现。"
    ),
    PredictionSource.LIVE_MODEL: ("预测来自本次真实模型调用，是唯一可以直接引用为系统能力的结论。"),
}

#: Printed with a live section whose recording is stale, and with any section whose
#: prompt version is not the live one.
STALE_NOTE = "提示词版本与当前版本不一致，该结论对应的是旧契约。"


@dataclass(frozen=True, slots=True)
class FixtureMetric:
    """One metric from a built-in hermetic suite (FIN-007/FIN-008)."""

    name: str
    value: float | None
    threshold: float | None
    passed: bool


@dataclass(frozen=True, slots=True)
class Pricing:
    """A recorded price list. Cost is only ever computed from one of these.

    ``version`` is mandatory and is printed with the estimate: a cost figure whose
    price list is unknown cannot be checked or updated, and an unlabelled number
    would be indistinguishable from a measured one.
    """

    version: str
    currency: str
    prompt_per_1k: float
    completion_per_1k: float

    def estimate(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens / 1000 * self.prompt_per_1k
            + completion_tokens / 1000 * self.completion_per_1k
        )


@dataclass(frozen=True, slots=True)
class Section:
    """One source's results. The report is an ordered list of these."""

    source: PredictionSource
    heading: str
    caveat: str
    metrics: CorpusMetrics | None = None
    fixtures: tuple[FixtureMetric, ...] = ()
    notes: tuple[str, ...] = ()
    pricing: Pricing | None = None


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    title: str
    sections: tuple[Section, ...]


def build_report(title: str, sections: list[Section]) -> EvaluationReport:
    """Assemble a report, refusing to let one source appear twice.

    Two sections for the same source would be two runs whose numbers a reader
    would naturally add up; raising forces the caller to say which one it means.
    """
    seen: set[PredictionSource] = set()
    for section in sections:
        if section.source in seen:
            raise ValueError(
                f"source {section.source.value} appears in more than one section; "
                "report one run per source instead of merging them"
            )
        seen.add(section.source)
    return EvaluationReport(title=title, sections=tuple(sections))


def render(report: EvaluationReport) -> str:
    """Render the report as Markdown, one section per source."""
    lines = [f"# {report.title}", ""]
    for section in report.sections:
        lines.extend(_render_section(section))
    return "\n".join(lines).rstrip() + "\n"


def _render_section(section: Section) -> list[str]:
    lines = [f"## {SOURCE_LABELS[section.source]}｜{section.heading}", ""]
    lines.append(f"> {section.caveat}")
    lines.append("")
    if section.metrics is not None:
        lines.extend(_render_corpus(section.metrics))
    if section.fixtures:
        lines.extend(_render_fixtures(section.fixtures))
    if section.metrics is not None:
        lines.extend(_render_cost(section.metrics, section.pricing))
    for note in section.notes:
        lines.append(f"- {note}")
    lines.append("")
    return lines


def _render_corpus(metrics: CorpusMetrics) -> list[str]:
    scope = metrics.split.value if metrics.split else "全部"
    lines = [
        "### 样本",
        "",
        f"- 数据集：`{metrics.corpus_name}@{metrics.corpus_version}`"
        f"（content_hash `{metrics.content_hash[:16]}…`）",
        f"- 划分：{scope}；用例 {metrics.measured_cases}/{metrics.total_cases} 例可测量，"
        f"失败 {metrics.failed_cases} 例",
        f"- 版本：提示词 `{metrics.prompt_version}`、规则 `{metrics.rule_version}`、"
        f"抽取网关 `{metrics.gateway_version}`、向量 `{metrics.embedding_version}`",
        f"- 检索池 {metrics.pool_size} 个画像，报告 K={metrics.top_k}",
        "",
        "### 抽取（字段级）",
        "",
        "| 字段 | 样本 | 精确率 | 召回率 | F1 | 完全命中 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for field in metrics.extraction.fields:
        lines.append(
            f"| {field.field} | {field.samples} | {field.precision:.3f} | "
            f"{field.recall:.3f} | {field.f1:.3f} | {field.exact_match:.3f} |"
        )
    lines.extend(
        [
            "",
            f"字段 F1 均值 **{metrics.extraction.overall_f1:.3f}**。"
            "技能名按大小写折叠比较：归一化是复核环节的职责，计在抽取头上会把归一化回归"
            "报成抽取错误。",
            "",
            "### 检索（按岗位）",
            "",
            "本节覆盖整个候选池，不受上面的划分限制：排序是候选池整体的性质，只排一半会得到一个"
            "生产环境不会产生的排序，而把池子切到每岗五个候选会让 Recall@K 恒等于 1，测到的是切法"
            "而不是检索器。",
            "",
            f"| 岗位 | 相关数 | 池大小 | 命中@{metrics.retrieval.k} "
            f"| Recall@{metrics.retrieval.k} "
            f"| 该岗位上限 | 首个相关位次 | nDCG@{metrics.retrieval.k} |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for family in metrics.retrieval.families:
        lines.append(
            f"| {family.job_family} | {family.relevant} | {family.pool_size} | "
            f"{family.retrieved_at_k} | {family.recall_at_k:.3f} | "
            f"{family.max_recall_at_k:.3f} | {family.first_relevant_rank or '—'} | "
            f"{family.ndcg_at_k:.3f} |"
        )
    lines.extend(
        [
            "",
            f"Recall@{metrics.retrieval.k} **{metrics.retrieval.recall_at_k:.3f}**"
            f"（上限 {metrics.retrieval.recall_ceiling:.3f}）｜"
            f"MRR **{metrics.retrieval.mrr:.3f}**｜"
            f"nDCG@{metrics.retrieval.k} **{metrics.retrieval.ndcg_at_k:.3f}**。",
            "",
            "上限是该岗位在 K 之下的理论最大值（`min(K, 相关数) / 相关数`）。相关候选多于 K 时"
            "Recall 无法接近 1，不写上限会让一个接近满额的数字看起来像失败。",
            "",
            "### 支持标签",
            "",
            "| 标签 | 标准答案 | 预测 | 命中 | 精确率 | 召回率 | F1 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in metrics.support.classes:
        lines.append(
            f"| {item.label} | {item.gold} | {item.predicted} | {item.true_positives} | "
            f"{item.precision:.3f} | {item.recall:.3f} | {item.f1:.3f} |"
        )
    lines.extend(
        [
            "",
            f"宏平均 F1 **{metrics.support.macro_f1:.3f}**、"
            f"宏平均精确率 **{metrics.support.macro_precision:.3f}**，"
            f"共 {metrics.support.samples} 个结论。宏平均只对标准答案或预测中出现过的标签求平均；"
            "语料里没有的标签不参与，否则「语料缺少某一档」会被记成系统缺陷。",
            "",
            "### 硬性条件结论",
            "",
            "| 规则 | 样本 | 一致 | 一致率 |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for rule in metrics.outcome.rules:
        lines.append(
            f"| {rule.rule_id} | {rule.samples} | {rule.agreements} | {rule.accuracy:.3f} |"
        )
    lines.extend(
        [
            "",
            f"整体一致率 **{metrics.outcome.accuracy:.3f}**。这一项不设阈值：硬性规则本就是"
            "确定性环节，不一致意味着语料或规格发生了移动，而不是模型发挥失常。",
            "",
            "### 失败案例",
            "",
        ]
    )
    if metrics.failure_count == 0:
        lines.append("无。")
    else:
        shown = len(metrics.failures)
        if shown < metrics.failure_count:
            lines.append(f"共 {metrics.failure_count} 条，下列前 {shown} 条。")
        else:
            lines.append(f"共 {metrics.failure_count} 条。")
        lines.extend(
            ["", "| 用例 | 阶段 | 对象 | 期望 | 实际 |", "| --- | --- | --- | --- | --- |"]
        )
        for sample in metrics.failures:
            lines.append(
                f"| {sample.case_id} | {_stage_label(sample.stage)} | {sample.subject} | "
                f"{sample.expected} | {sample.observed} |"
            )
    lines.extend(["", f"总耗时 {metrics.elapsed_seconds:.2f} 秒。", ""])
    return lines


_STAGE_LABELS: dict[Stage, str] = {
    Stage.RUN: "运行",
    Stage.EXTRACTION: "抽取",
    Stage.RETRIEVAL: "检索",
    Stage.OUTCOME: "结论",
    Stage.SUPPORT: "支持标签",
}


def _stage_label(stage: Stage) -> str:
    return _STAGE_LABELS[stage]


def _render_fixtures(fixtures: tuple[FixtureMetric, ...]) -> list[str]:
    lines = [
        "### 内置套件指标",
        "",
        "| 指标 | 数值 | 阈值 | 判定 |",
        "| --- | ---: | ---: | --- |",
    ]
    for item in fixtures:
        value = "—" if item.value is None else f"{item.value:.4f}"
        threshold = "—" if item.threshold is None else f"{item.threshold:.4f}"
        lines.append(
            f"| {item.name} | {value} | {threshold} | {'通过' if item.passed else '未通过'} |"
        )
    lines.append("")
    return lines


def _render_cost(metrics: CorpusMetrics, pricing: Pricing | None) -> list[str]:
    lines = ["### 耗时与用量", ""]
    usage = metrics.usage
    if usage is None:
        lines.append("- Token 用量：本网关不上报用量，因此**未记录**（不估算）。")
    else:
        lines.append(
            f"- Token 用量：{usage.calls_with_usage}/{usage.calls} 次调用上报用量；"
            f"prompt {usage.prompt_tokens}、completion {usage.completion_tokens}，"
            f"合计 {usage.total_tokens}。"
        )
        if not usage.complete:
            lines.append("  - 部分调用未上报用量，上述合计是已知部分而非总量，不能作为计费依据。")
    if pricing is None:
        lines.append(
            "- 费用：**未估算**。没有明确记录的计价版本时不给金额，"
            "否则一个无法核对的价格会被当成实测值。"
        )
    elif usage is None:
        lines.append("- 费用：无法估算（本网关不上报用量）。")
    else:
        amount = pricing.estimate(usage.prompt_tokens, usage.completion_tokens)
        suffix = "" if usage.complete else "（基于部分用量，仅作下界）"
        lines.append(
            f"- 费用：约 {amount:.4f} {pricing.currency}"
            f"（计价版本 `{pricing.version}`，prompt {pricing.prompt_per_1k}/1k、"
            f"completion {pricing.completion_per_1k}/1k）{suffix}。"
        )
    lines.append("")
    return lines


__all__ = [
    "SOURCE_CAVEATS",
    "SOURCE_LABELS",
    "STALE_NOTE",
    "EvaluationReport",
    "FixtureMetric",
    "Pricing",
    "Section",
    "build_report",
    "render",
]
