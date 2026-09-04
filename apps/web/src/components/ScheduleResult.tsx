import { StatusBadge } from "./RunStatusBadge"

interface Proposal {
  application_id: string
  duration_minutes: number
  timezone: string
  interviewer_label: string
}

/** Mock schedule confirmation created after the second approval (§15.4). */
export function ScheduleResult({
  proposal,
  status,
}: {
  proposal: Proposal | null
  status: string | null
}) {
  if (proposal === null) {
    return <p className="muted">尚未创建面试安排。</p>
  }
  return (
    <div className="run-stack">
      <div className="button-row" style={{ marginTop: 0 }}>
        <StatusBadge kind="approval" status={status ?? "PENDING"} />
      </div>
      <dl className="kv">
        <dt>时长</dt>
        <dd>{proposal.duration_minutes} 分钟</dd>
        <dt>时区</dt>
        <dd>{proposal.timezone}</dd>
        <dt>面试官</dt>
        <dd>{proposal.interviewer_label}</dd>
        <dt>关联申请</dt>
        <dd><code>{proposal.application_id.slice(0, 8)}</code></dd>
      </dl>
    </div>
  )
}
