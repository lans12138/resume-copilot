/**
 * What an approval will do, what changed, and what happened (PORT-005).
 *
 * The approval screen is where a human authorises a write, so it has to answer four
 * questions without the reader assembling them: what exactly will be applied, what did
 * I change, which version am I deciding against, and what was the result. It used to
 * answer none of them — `ActionDiff` printed two columns of raw payload keys
 * (`target_status`, `interviewer_label`) and the status was a bare badge.
 *
 * The parameter vocabulary here is the backend's own (`job_applications/graph.py`
 * proposals, `side_effects.py` execution). An unknown key or value renders as itself:
 * a payload field added later must be visible, even if unlabelled, because a reviewer
 * approving a write they cannot read is worse than one reading a raw key.
 */

import type { ApprovalActionType, ApprovalStatus } from "../api/types"

export type ApprovalTone = "neutral" | "info" | "warn" | "ok" | "danger" | "muted"

/** The action an approval performs, in the words a reviewer uses. */
export const ACTION_LABELS: Record<ApprovalActionType, string> = {
  UPDATE_APPLICATION_STATUS: "更新申请状态",
  CREATE_INTERVIEW_SCHEDULE: "创建面试安排",
}

/** One line saying what the action does, so the diff has a subject. */
export const ACTION_SUMMARIES: Record<ApprovalActionType, string> = {
  UPDATE_APPLICATION_STATUS: "把候选人的申请状态改为提案中的目标状态。",
  CREATE_INTERVIEW_SCHEDULE: "为候选人创建一条面试安排，并写入外部排期编号。",
}

/** Application lifecycle states (§4.5). */
const APPLICATION_STATUS_LABELS: Record<string, string> = {
  CREATED: "已创建",
  SHORTLISTED: "已入围",
  ON_HOLD: "暂缓",
  REJECTED: "已淘汰",
  INTERVIEW_SCHEDULED: "已安排面试",
}

/** Parameter keys as they appear in the proposal payload. */
const PARAM_LABELS: Record<string, string> = {
  target_status: "目标状态",
  application_id: "申请",
  duration_minutes: "时长（分钟）",
  timezone: "时区",
  interviewer_label: "面试官",
}

export function actionLabel(actionType: ApprovalActionType): string {
  return ACTION_LABELS[actionType] ?? actionType
}

export function paramLabel(key: string): string {
  return PARAM_LABELS[key] ?? key
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

/**
 * Render one parameter value for a human.
 *
 * A status is translated *and* kept in its stored form: "已入围" is what the reader
 * decides on, and "SHORTLISTED" is what the audit trail and the API will say, so the
 * screen shows both rather than making the operator map one to the other.
 */
export function paramValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—"
  if (key === "target_status" && typeof value === "string") {
    const label = APPLICATION_STATUS_LABELS[value]
    return label ? `${label}（${value}）` : value
  }
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return JSON.stringify(value)
}

/** One sentence naming the concrete effect of the proposed parameters. */
export function describeAction(
  actionType: ApprovalActionType,
  params: Record<string, unknown> | null,
): string {
  const values = params ?? {}
  if (actionType === "UPDATE_APPLICATION_STATUS") {
    const target = values.target_status
    if (typeof target === "string" && target !== "") {
      return `将申请状态更新为「${APPLICATION_STATUS_LABELS[target] ?? target}」。`
    }
    return "将申请状态更新为提案中的目标状态。"
  }
  if (actionType === "CREATE_INTERVIEW_SCHEDULE") {
    const duration = values.duration_minutes
    const timezone = values.timezone
    const interviewer = values.interviewer_label
    const parts: string[] = []
    if (typeof duration === "number") parts.push(`${duration} 分钟`)
    if (typeof timezone === "string" && timezone !== "") parts.push(`时区 ${timezone}`)
    if (typeof interviewer === "string" && interviewer !== "") parts.push(`面试官 ${interviewer}`)
    return parts.length > 0
      ? `创建面试安排：${parts.join(" · ")}。`
      : "创建一条面试安排。"
  }
  return ACTION_SUMMARIES[actionType] ?? "执行提案中的动作。"
}

export type ParamChangeKind = "same" | "changed" | "added" | "removed"

export interface ParamChange {
  key: string
  label: string
  /** Rendered original value, or null when the key is new. */
  before: string | null
  /** Rendered final value, or null when the key was dropped. */
  after: string | null
  kind: ParamChangeKind
}

/**
 * Compare the proposal with what will actually be applied.
 *
 * Both sides are walked, not just the final one: a key the reviewer *removed* is a
 * change, and a diff that only listed the surviving keys would show it as unchanged
 * silence. Ordering follows the original proposal first so an edit reads as a
 * modification of something rather than a new list.
 */
export function diffParams(
  original: Record<string, unknown> | null,
  final: Record<string, unknown> | null,
): ParamChange[] {
  const before = original ?? {}
  const after = final ?? {}
  const keys = [...Object.keys(before), ...Object.keys(after).filter((k) => !(k in before))]

  return keys.map((key) => {
    const hadBefore = key in before
    const hasAfter = key in after
    const beforeText = hadBefore ? paramValue(key, before[key]) : null
    const afterText = hasAfter ? paramValue(key, after[key]) : null
    let kind: ParamChangeKind = "same"
    if (hadBefore && !hasAfter) kind = "removed"
    else if (!hadBefore && hasAfter) kind = "added"
    else if (beforeText !== afterText) kind = "changed"
    return { key, label: paramLabel(key), before: beforeText, after: afterText, kind }
  })
}

export interface OutcomeView {
  label: string
  tone: ApprovalTone
  /** What this status means for the data. */
  detail: string
  /** The next step, when there is one. */
  guidance: string | null
}

/**
 * What each approval status means for the data, and what to do next.
 *
 * The distinction that matters and was previously invisible: APPROVED/EDITED means
 * the *decision* is recorded but the write has not happened yet — execution is a
 * separate step — while EXECUTED means the write landed exactly once. Showing both as
 * a bare "已通过" would let an operator leave the screen believing a status change had
 * been applied when the executor had not yet run.
 */
const OUTCOMES: Record<ApprovalStatus, OutcomeView> = {
  PENDING: {
    label: "等待决策",
    tone: "warn",
    detail: "通过后执行上述动作；驳回则不写入任何数据。",
    guidance: null,
  },
  APPROVED: {
    label: "已通过，等待执行",
    tone: "info",
    detail: "决策已记录，副作用由执行器应用，尚未写入。",
    guidance: "本页会自动刷新执行结果；若长时间停在此状态，请到申请流程查看状态。",
  },
  EDITED: {
    label: "已修改并通过，等待执行",
    tone: "info",
    detail: "将按修改后的参数执行，副作用由执行器应用，尚未写入。",
    guidance: "本页会自动刷新执行结果；若长时间停在此状态，请到申请流程查看状态。",
  },
  EXECUTED: {
    label: "已执行",
    tone: "ok",
    detail: "动作已应用，且只应用了一次。",
    guidance: null,
  },
  EXECUTION_FAILED: {
    label: "执行失败",
    tone: "danger",
    detail: "动作没有应用，不存在部分写入。",
    guidance: "到申请流程重试：流程会以新的尝试重新执行这次动作。",
  },
  REJECTED: {
    label: "已驳回",
    tone: "danger",
    detail: "没有执行任何写入。",
    guidance: null,
  },
  EXPIRED: {
    label: "已过期",
    tone: "muted",
    detail: "超时未决策，审批已失效，没有执行任何写入。",
    guidance: "如需继续，请从申请流程重新发起。",
  },
}

export function describeOutcome(status: ApprovalStatus): OutcomeView {
  return (
    OUTCOMES[status] ?? {
      label: status,
      tone: "muted",
      detail: "该状态没有对应的说明，请以服务端状态为准。",
      guidance: null,
    }
  )
}

/** True while the side effect has not been applied yet, so the page should poll. */
export function awaitingExecution(status: ApprovalStatus): boolean {
  return status === "APPROVED" || status === "EDITED"
}

/** True when the approval can no longer be decided (§12.5). */
export function isDecidable(status: ApprovalStatus): boolean {
  return status === "PENDING"
}
