import type { ApprovalActionType } from "../api/types"

function ParamsView({ params }: { params: Record<string, unknown> | null }) {
  if (params === null || Object.keys(params).length === 0) {
    return <p className="muted">无参数</p>
  }
  return (
    <div>
      {Object.entries(params).map(([key, value]) => (
        <div className="param-row" key={key}>
          <span>{key}</span>
          <code>{typeof value === "object" ? JSON.stringify(value) : String(value)}</code>
        </div>
      ))}
    </div>
  )
}

const actionLabel: Record<ApprovalActionType, string> = {
  UPDATE_APPLICATION_STATUS: "更新申请状态",
  CREATE_INTERVIEW_SCHEDULE: "创建面试安排",
}

/** Side-by-side view of the original proposal vs. the (edited) final params (§15.5). */
export function ActionDiff({
  actionType,
  originalParams,
  finalParams,
}: {
  actionType: ApprovalActionType
  originalParams: Record<string, unknown> | null
  finalParams: Record<string, unknown> | null
}) {
  return (
    <div className="run-stack">
      <p className="eyebrow">Action · {actionLabel[actionType]}</p>
      <div className="diff-grid">
        <div className="diff-col">
          <h4>原提案</h4>
          <ParamsView params={originalParams} />
        </div>
        <div className="diff-col">
          <h4>编辑后参数</h4>
          <ParamsView params={finalParams} />
        </div>
      </div>
    </div>
  )
}
