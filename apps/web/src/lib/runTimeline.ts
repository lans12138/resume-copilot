/**
 * Readable names for the run timeline (PORT-005).
 *
 * The timeline used to print the event type, then the raw graph node id
 * (`score_with_evidence`), then the raw message key (`node.score_with_evidence.failed`).
 * An operator reading it had to translate three vocabularies at once, and the one
 * thing they actually needed — which candidate failed, and why — was buried in the
 * payload. This module keeps the two audiences apart: a title and a status a
 * presenter can read aloud, a failure sentence when something broke, and every
 * identifier an engineer might need tucked into `technical` for a collapsed panel.
 *
 * The label maps are keyed by the *backend's* own vocabulary (`graph.py`,
 * `agent/models.py`, the `message_key=` literals). They are not exhaustive on
 * purpose: an unknown node, message key or failure token renders as itself. A
 * timeline that silently showed "未知事件" for a node added later would hide the very
 * thing it exists to surface, so the fallback is the raw value, never a placeholder.
 */

import type { AgentEventEnvelope } from "../api/types"

export type TimelineTone = "neutral" | "info" | "warn" | "ok" | "danger" | "muted"

/** Run / candidate processing status, as shown on a row. */
const STATUS_LABELS: Record<string, string> = {
  CREATED: "已创建",
  RUNNING: "运行中",
  WAITING_APPROVAL: "等待审批",
  INTERRUPTED: "已中断",
  COMPLETED: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
  PENDING: "待处理",
}

const STATUS_TONES: Record<string, TimelineTone> = {
  CREATED: "neutral",
  RUNNING: "info",
  WAITING_APPROVAL: "warn",
  INTERRUPTED: "warn",
  COMPLETED: "ok",
  FAILED: "danger",
  CANCELLED: "muted",
  PENDING: "neutral",
}

/** Agent event types (§13.1). */
const EVENT_LABELS: Record<string, string> = {
  RUN_CREATED: "流程创建",
  NODE_STARTED: "节点开始",
  NODE_COMPLETED: "节点完成",
  RUN_RESUMED: "流程恢复",
  STATUS_CHANGED: "状态变更",
  RUN_COMPLETED: "流程完成",
  RUN_FAILED: "流程失败",
  RUN_CANCELLED: "流程取消",
}

const EVENT_TONES: Record<string, TimelineTone> = {
  RUN_CREATED: "neutral",
  NODE_STARTED: "info",
  NODE_COMPLETED: "neutral",
  RUN_RESUMED: "info",
  STATUS_CHANGED: "info",
  RUN_COMPLETED: "ok",
  RUN_FAILED: "danger",
  RUN_CANCELLED: "muted",
}

/** Graph node ids → what the node does (§10 ApplicationRun, §10.2 MatchRun). */
const NODE_LABELS: Record<string, string> = {
  load_application: "载入申请",
  analyze_candidate: "分析候选人",
  human_review: "等待人工审批",
  update_application_status: "更新申请状态",
  generate_interview_questions: "生成面试问题",
  wait_schedule_approval: "等待排期审批",
  create_interview_schedule: "创建面试安排",
  finalize_run: "收尾",
  retrieve_candidates: "召回候选人",
  hard_rule_evaluate: "硬规则判定",
  score_with_evidence: "证据化评分",
  aggregate_run: "汇总结果",
}

/** Message keys → the sentence a reader sees. */
const MESSAGE_LABELS: Record<string, string> = {
  "run.created": "流程已创建，等待执行",
  "run.worker_started": "执行器已接管本次流程",
  "run.resumed": "流程已恢复执行",
  "run.resumed_after_approval": "审批通过后流程继续执行",
  "run.interrupted": "流程在人工审批处暂停，等待决策",
  "run.rejected": "人工审批驳回，流程结束且没有写入副作用",
  "run.completed": "流程完成",
  "run.failed": "流程失败",
  "run.cancelled": "流程已取消",
  "approval.created": "已创建待决策的审批",
  "approval.decided": "人工已完成决策",
  "tool.executed": "执行了一次受控副作用",
  "match_run.created": "批量分析已创建",
  "match_run.completed": "批量分析完成",
  "match_run.failed": "批量分析失败",
}

/**
 * Failure tokens the backend emits today, from `mark_failed(reason=..., error_code=...)`.
 *
 * Only values that exist in the codebase are listed. A token that is not here is
 * shown as-is next to the generic sentence — guessing a meaning for an unknown code
 * would be worse than admitting the UI does not know it.
 */
const FAILURE_LABELS: Record<string, string> = {
  user_cancel: "由用户取消",
  APPROVAL_EXPIRED: "审批超时未决策，流程已失败",
  schedule_backend_unavailable: "排期后端不可用，面试安排未创建",
  SCHEDULE_BACKEND_UNAVAILABLE: "排期后端不可用，面试安排未创建",
  CANDIDATE_SCORING_FAILED: "该候选人评分失败，其余候选人不受影响",
  ACTION_REJECTED: "人工审批驳回",
  RUN_CANCELLED: "流程被取消",
}

/** `NODE_STARTED` / `NODE_COMPLETED` → the phase appended to the node's name. */
const NODE_PHASES: Record<string, string> = {
  NODE_STARTED: "开始",
  NODE_COMPLETED: "完成",
}

/** Events whose own name is the title, whatever node they carry. */
const SELF_TITLED: ReadonlySet<string> = new Set([
  "RUN_FAILED",
  "RUN_COMPLETED",
  "RUN_CANCELLED",
  "RUN_CREATED",
  "RUN_RESUMED",
])

/**
 * The headline for a terminal event, keyed by the run-level message key.
 *
 * The event type alone cannot name the outcome: a rejected approval and a completed
 * run both emit `RUN_COMPLETED`, and titling the rejection "流程完成" would hide the
 * decision that ended it. The message key is the backend's own name for the outcome,
 * so it — not the event type — decides the headline.
 */
const OUTCOME_LABELS: Record<string, string> = {
  "run.completed": "流程完成",
  "run.failed": "流程失败",
  "run.cancelled": "流程已取消",
  "run.rejected": "审批驳回，流程结束",
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

export function statusTone(status: string): TimelineTone {
  return STATUS_TONES[status] ?? "muted"
}

export function nodeLabel(node: string): string {
  return NODE_LABELS[node] ?? node
}

export function messageLabel(key: string): string {
  return MESSAGE_LABELS[key] ?? key
}

/** The identifiers a reader does not need but an engineer does. */
export interface TimelineTechnical {
  event_type: string
  node: string | null
  message_key: string
  /** `safe_payload` as JSON, or null when the event carries nothing. */
  payload: string | null
}

export interface TimelineFailure {
  summary: string
  /** The machine-readable code, shown as a chip so it can be quoted in a ticket. */
  code: string | null
  /** Which candidate it happened to, when the failure is per-candidate. */
  candidate: string | null
}

export interface TimelineRow {
  title: string
  /** A sentence of context, when the event adds something the title does not say. */
  detail: string | null
  status: string
  tone: TimelineTone
  failure: TimelineFailure | null
  technical: TimelineTechnical
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null
}

/** A failure payload, or null when the event did not fail anything. */
function readFailure(event: AgentEventEnvelope): TimelineFailure | null {
  const payload = event.safe_payload ?? {}
  const code = asString(payload.error_code)
  const reason = asString(payload.reason)
  // A node can fail while the run continues (`score_with_evidence` isolates one
  // candidate's fault), so the status is checked as well as the event type: a
  // completed run must not show a failure row as if the whole run broke.
  const failed =
    event.event_type === "RUN_FAILED" || event.status === "FAILED" || code !== null
  if (!failed) return null

  const token = code ?? reason
  const mapped = token === null ? null : FAILURE_LABELS[token]
  const summary =
    mapped ??
    (event.event_type === "RUN_FAILED" ? "流程失败" : "该节点失败")
  return {
    summary,
    code: token,
    candidate: asString(payload.candidate_profile_id)?.slice(0, 8) ?? null,
  }
}

/**
 * The reason behind a non-failing terminal event — a cancellation, a rejection.
 *
 * These are not failures, so they get no failure row, but the reason is the only
 * place the *why* is recorded and dropping it would leave "流程已取消" with nothing
 * behind it. Unmapped tokens return null: an operator reads "user_cancel" as a code,
 * not as an explanation, and the raw token is in the payload for them.
 */
function readReason(event: AgentEventEnvelope): string | null {
  if (event.event_type === "RUN_FAILED" || event.status === "FAILED") return null
  const reason = asString((event.safe_payload ?? {}).reason)
  return reason === null ? null : (FAILURE_LABELS[reason] ?? null)
}

/**
 * Turn one wire event into a row a person can read.
 *
 * Title priority is deliberate: a terminal event is named by what happened to the
 * *run* ("流程失败"), not by the node it happened at, because the node name would
 * make an operator look for a local problem where the run actually ended.
 */
export function describeEvent(event: AgentEventEnvelope): TimelineRow {
  // Only a *mapped* message is ever spoken. An unmapped key (a `node.*` key, or one
  // a newer backend added) must never leak into the reading line — printing
  // `node.score_with_evidence.completed` as an explanation is exactly the problem
  // this module exists to remove, and the title already names the node.
  const message = MESSAGE_LABELS[event.message_key] ?? null
  const node = event.node

  let title: string
  if (SELF_TITLED.has(event.event_type)) {
    title = OUTCOME_LABELS[event.message_key] ?? EVENT_LABELS[event.event_type] ?? event.event_type
  } else if (node !== null) {
    const phase = NODE_PHASES[event.event_type]
    title = phase ? `${nodeLabel(node)} · ${phase}` : nodeLabel(node)
  } else {
    title = message ?? EVENT_LABELS[event.event_type] ?? event.event_type
  }

  // Only say the message when it adds something. A STATUS_CHANGED carrying
  // "run.interrupted" beside the node "等待人工审批" explains why it paused; the same
  // sentence repeated under an identical title would be noise.
  const failure = readFailure(event)
  const detail =
    message !== null && message !== title ? message : failure === null ? readReason(event) : null

  const payload = event.safe_payload ?? {}
  const technical: TimelineTechnical = {
    event_type: event.event_type,
    node,
    message_key: event.message_key,
    payload: Object.keys(payload).length > 0 ? JSON.stringify(payload) : null,
  }

  return {
    title,
    detail,
    status: statusLabel(event.status),
    tone: EVENT_TONES[event.event_type] ?? statusTone(event.status),
    failure,
    technical,
  }
}
