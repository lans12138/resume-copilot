/** Approve / Edit / Reject controls for an approval (§15.5, keyboard accessible). */
export function DecisionActions({
  onApprove,
  onEdit,
  onReject,
  editing,
  busy,
  disabled,
}: {
  onApprove: () => void
  onEdit: () => void
  onReject: () => void
  editing: boolean
  busy: boolean
  disabled: boolean
}) {
  return (
    <div className="button-row" role="group" aria-label="审批决策">
      <button className="button button-primary" type="button" disabled={disabled || busy} onClick={onApprove}>
        通过
      </button>
      <button className="button button-ghost" type="button" disabled={disabled || busy} aria-pressed={editing} onClick={onEdit}>
        {editing ? "继续编辑" : "修改参数"}
      </button>
      <button className="button button-danger" type="button" disabled={disabled || busy} onClick={onReject}>
        驳回
      </button>
    </div>
  )
}
