import { Link } from "react-router-dom"
import type { ApprovalDetail } from "../api/types"
import { actionLabel, describeAction, describeOutcome } from "../lib/approval"
import { ApprovalStatusBadge } from "./RunStatusBadge"

/**
 * The pending approval for an ApplicationRun, if any. The server decides which
 * approval is operable — the UI never infers it (§12.5).
 *
 * PORT-005: the card used to be a badge and a link, so the run page said an approval
 * existed without saying what it would do. It now names the action and the concrete
 * effect, because "更新申请状态" alone does not tell a reader whether the candidate is
 * about to be shortlisted or rejected.
 */
export function CurrentApprovalCard({ approval }: { approval: ApprovalDetail | null }) {
  if (approval === null) {
    return (
      <div className="empty-state">
        <strong>该流程当前没有等待决策的审批</strong>
        <span>流程暂停时会在这里出现待决策的审批；已完成或已驳回的流程不会再有。</span>
      </div>
    )
  }
  const outcome = describeOutcome(approval.status)

  return (
    <div className="run-stack">
      <div className="button-row" style={{ marginTop: 0 }}>
        <ApprovalStatusBadge status={approval.status} />
        <span className="badge badge-neutral">{actionLabel(approval.action_type)}</span>
        <span className="badge badge-muted">审批版本 v{approval.version}</span>
      </div>
      <p className="action-summary">
        {describeAction(approval.action_type, approval.final_params ?? approval.original_params)}
      </p>
      <p className="muted">{outcome.detail}</p>
      <Link className="back-link" to={`/approvals/${approval.id}`}>查看并决策 →</Link>
    </div>
  )
}
