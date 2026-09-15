import type { EvaluationKind, EvaluationStatus } from "../api/types"

/**
 * Presentation mapping for the evaluation results page (FIN-007).
 *
 * The page's job is to answer one question fast: *is the gate green, and if not,
 * which bar did it miss?* So the labels are explicit per status and per kind, and
 * a metric's verdict is rendered from its stored `passed`/`threshold` rather than
 * recomputed — the API is the single source of truth for the verdict, and a second
 * implementation here would be a second thing to keep in sync.
 */

const STATUS_LABELS: Record<EvaluationStatus, string> = {
  CREATED: "已排队",
  RUNNING: "评测中",
  COMPLETED: "已完成",
  FAILED: "执行失败",
}

const KIND_LABELS: Record<EvaluationKind, string> = {
  GOLDEN: "检索 Golden",
  SEMANTIC: "语义支持",
  INJECTION: "注入防护",
}

/** Terminal states never change again; a re-run is a new record. */
export function isTerminalEvaluationStatus(status: EvaluationStatus): boolean {
  return status === "COMPLETED" || status === "FAILED"
}

export function evaluationStatusLabel(status: EvaluationStatus): string {
  return STATUS_LABELS[status] ?? status
}

export function evaluationKindLabel(kind: EvaluationKind): string {
  // An unknown kind falls through to the raw value rather than "未知": the raw
  // code is what lets a reader search the backend for its meaning.
  return KIND_LABELS[kind] ?? kind
}

/**
 * The run-level verdict as a short label.
 *
 * `null` means "no verdict yet" and must stay visually distinct from "failed" —
 * a queued run is not a failing one, and conflating them would make an in-flight
 * evaluation look like a regression.
 */
export function evaluationVerdictLabel(passed: boolean | null, status: EvaluationStatus): string {
  if (status === "FAILED") return "执行失败"
  if (passed === null) return "尚无结论"
  return passed ? "通过" : "未达标"
}

export function evaluationVerdictTone(passed: boolean | null, status: EvaluationStatus): "pass" | "fail" | "pending" {
  if (status === "FAILED") return "fail"
  if (passed === null) return "pending"
  return passed ? "pass" : "fail"
}

// Metric-name display mapping. Kept explicit so a new metric shows its raw name
// rather than an empty cell — an unlabelled number is worse than an ugly one.
const METRIC_LABELS: Record<string, string> = {
  recall_at_k: "召回率 Recall@K",
  mrr: "MRR",
  ndcg_at_k: "NDCG@K",
  supported_precision: "SUPPORTED 精确率",
  support_macro_f1: "三分类宏平均 F1",
  illegal_reference_count: "非法引用数",
  injection_control_flow_changes: "注入改变控制流",
  injection_unauthorized_tool_proposals: "越权工具提案",
  injection_approval_bypasses: "审批绕过",
  injection_attack_target_hits: "攻击目标命中",
  injection_paired_hard_rule_changes: "配对硬规则变化",
  injection_new_unsupported_high_impact_claims: "新增无支持高影响结论",
}

export function metricLabel(metricName: string): string {
  return METRIC_LABELS[metricName] ?? metricName
}

/**
 * Format a metric value for display.
 *
 * Counts are rendered as integers: "3.000000" for an attack-success counter reads
 * like a rate and invites the wrong interpretation, while "3" is unambiguously a
 * tally.
 */
export function formatMetricValue(value: number, metricName: string): string {
  if (metricName.endsWith("_count") || metricName.startsWith("injection_")) {
    return Number.isInteger(value) ? String(value) : value.toFixed(2)
  }
  return value.toFixed(4)
}

export function formatThreshold(threshold: number | null, metricName: string): string {
  if (threshold === null) return "—"
  if (metricName.endsWith("_count") || metricName.startsWith("injection_")) {
    return Number.isInteger(threshold) ? String(threshold) : threshold.toFixed(2)
  }
  return threshold.toFixed(2)
}

/**
 * Whether a metric is a ceiling (lower is better).
 *
 * Used only for the human-readable comparison hint; the pass/fail verdict itself
 * always comes from the server.
 */
export function isCeilingMetric(metricName: string): boolean {
  return metricName === "illegal_reference_count" || metricName.startsWith("injection_")
}
