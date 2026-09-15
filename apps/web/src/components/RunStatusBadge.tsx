import type { ApprovalStatus, InterviewStatus, RunStatus } from "../api/types"

type Tone = "neutral" | "info" | "warn" | "ok" | "danger" | "muted"

const runLabel: Record<RunStatus, string> = {
  CREATED: "已创建",
  RUNNING: "运行中",
  WAITING_APPROVAL: "等待审批",
  INTERRUPTED: "已中断",
  COMPLETED: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
}
const runTone: Record<RunStatus, Tone> = {
  CREATED: "neutral",
  RUNNING: "info",
  WAITING_APPROVAL: "warn",
  INTERRUPTED: "warn",
  COMPLETED: "ok",
  FAILED: "danger",
  CANCELLED: "muted",
}
const approvalLabel: Record<ApprovalStatus, string> = {
  PENDING: "待决策",
  APPROVED: "已通过",
  EDITED: "已修改",
  REJECTED: "已驳回",
  EXECUTED: "已执行",
  EXECUTION_FAILED: "执行失败",
  EXPIRED: "已过期",
}
const approvalTone: Record<ApprovalStatus, Tone> = {
  PENDING: "warn",
  APPROVED: "ok",
  EDITED: "info",
  REJECTED: "danger",
  EXECUTED: "ok",
  EXECUTION_FAILED: "danger",
  EXPIRED: "muted",
}
const interviewLabel: Record<InterviewStatus, string> = {
  SCHEDULED: "已排期",
  CANCELLED: "已取消",
}
const interviewTone: Record<InterviewStatus, Tone> = {
  SCHEDULED: "ok",
  CANCELLED: "muted",
}

export function RunStatusBadge({ status }: { status: RunStatus }) {
  return <span className={`badge badge-${runTone[status]}`}>{runLabel[status]}</span>
}

export function ApprovalStatusBadge({ status }: { status: ApprovalStatus }) {
  return <span className={`badge badge-${approvalTone[status]}`}>{approvalLabel[status]}</span>
}

export function InterviewStatusBadge({ status }: { status: InterviewStatus }) {
  return <span className={`badge badge-${interviewTone[status]}`}>{interviewLabel[status]}</span>
}

export function StatusBadge({ kind, status }: { kind: "run" | "approval"; status: string }) {
  if (kind === "run") return <RunStatusBadge status={status as RunStatus} />
  return <ApprovalStatusBadge status={status as ApprovalStatus} />
}
