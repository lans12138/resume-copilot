import { useState, type FormEvent } from "react"

/**
 * Editor for the approval's final params. Params are arbitrary bounded JSON from
 * the server, so we expose a validated JSON textarea rather than a fixed form
 * (the server re-validates on decision; §15.5).
 */
export function EditableParamsForm({
  initial,
  onApply,
}: {
  initial: Record<string, unknown> | null
  onApply: (params: Record<string, unknown>) => void
}) {
  const [text, setText] = useState(() => JSON.stringify(initial ?? {}, null, 2))
  const [error, setError] = useState<string | null>(null)

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    try {
      const parsed = JSON.parse(text)
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("参数必须是 JSON 对象")
      }
      setError(null)
      onApply(parsed as Record<string, unknown>)
    } catch (err) {
      setError(err instanceof Error ? err.message : "JSON 解析失败")
    }
  }

  return (
    <form className="form-stack" onSubmit={submit}>
      <label htmlFor="edit-params">编辑后参数（JSON）</label>
      <textarea id="edit-params" rows={8} value={text} onChange={(e) => setText(e.target.value)} />
      {error ? <p className="field-error">{error}</p> : null}
      <button className="button button-primary" type="submit">应用修改</button>
    </form>
  )
}
