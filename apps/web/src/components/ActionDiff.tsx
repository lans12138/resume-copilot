import { diffParams, type ParamChange } from "../lib/approval"

const changeLabel: Record<ParamChange["kind"], string> = {
  same: "未改动",
  changed: "已修改",
  added: "新增",
  removed: "已删除",
}

const changeTone: Record<ParamChange["kind"], string> = {
  same: "muted",
  changed: "info",
  added: "ok",
  removed: "danger",
}

/**
 * Original proposal vs. the (edited) final parameters (§15.5).
 *
 * PORT-005: the columns used to be two independent dumps of the payload, so the
 * reviewer had to compare them by eye — and a parameter that had been *deleted*
 * simply vanished from the right-hand column, which reads as "unchanged" rather than
 * as a change. Rows are now aligned by key and labelled with what happened to each
 * one.
 *
 * The action itself is not restated here: the panel above names it and says what it
 * will apply, and repeating that sentence under a second heading made the page look
 * like it was showing two proposals.
 */
export function ActionDiff({
  originalParams,
  finalParams,
}: {
  originalParams: Record<string, unknown> | null
  finalParams: Record<string, unknown> | null
}) {
  const changes = diffParams(originalParams, finalParams)
  if (changes.length === 0) {
    return <p className="muted">该动作没有参数。</p>
  }
  const edited = changes.some((change) => change.kind !== "same")

  return (
    <table className="diff-table">
      <thead>
        <tr>
          <th scope="col">参数</th>
          <th scope="col">原提案</th>
          <th scope="col">{edited ? "修改后" : "当前"}</th>
          <th scope="col">变化</th>
        </tr>
      </thead>
      <tbody>
        {changes.map((change) => (
          <tr key={change.key} className={change.kind === "same" ? undefined : "is-changed"}>
            <th scope="row">{change.label}</th>
            <td>{change.before === null ? <span className="muted">—</span> : <code>{change.before}</code>}</td>
            <td>{change.after === null ? <span className="muted">—</span> : <code>{change.after}</code>}</td>
            <td>
              <span className={`badge badge-${changeTone[change.kind]}`}>
                {changeLabel[change.kind]}
              </span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
