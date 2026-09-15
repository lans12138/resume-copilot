import { Link } from "react-router-dom"
import type { ApprovalDetail } from "../api/types"
import { ApprovalStatusBadge } from "./RunStatusBadge"

const actionLabel: Record<ApprovalDetail["action_type"], string> = {
  UPDATE_APPLICATION_STATUS: "更新申请状态",
  CREATE_INTERVIEW_SCHEDULE: "创建面试安排",
}

/**
 * The pending approval for an ApplicationRun, if any. The server decides which
 * approval is operable — the UI never infers it (§12.5).
 */
export function CurrentApprovalCard({ approval }: { approval: ApprovalDetail | null }) {
  if (approval === null) {
    return <p className="muted">该流程当前没有等待决策的审批。</p>
  }
  return (
    <div className="run-stack">
      <div className="button-row" style={{ marginTop: 0 }}>
        <ApprovalStatusBadge status={approval.status} />
        <span className="badge badge-neutral">{actionLabel[approval.action_type]}</span>
      </div>
      <Link className="back-link" to={`/approvals/${approval.id}`}>查看并决策 →</Link>
    </div>
  )
}
