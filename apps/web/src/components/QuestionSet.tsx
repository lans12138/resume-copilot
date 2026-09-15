interface QuestionItem {
  question_id?: string
  question_text?: string
  competency?: string
  rationale?: string
  evidence_chunk_ids?: string[]
  sensitivity_flags?: string[]
}

/**
 * Structured interview questions generated for an ApplicationRun (§15.4). The
 * payload is opaque JSON from the server; we only render the bounded fields and
 * surface any sensitivity flags the guard raised.
 */
export function QuestionSet({ questionSet }: { questionSet: Record<string, unknown> | null }) {
  if (questionSet === null) {
    return <p className="muted">该流程尚未生成面试问题集。</p>
  }
  const questions = (questionSet.questions as QuestionItem[] | undefined) ?? []
  if (questions.length === 0) {
    return <p className="muted">问题集为空。</p>
  }
  return (
    <div>
      {questions.map((q, idx) => {
        const sensitive = (q.sensitivity_flags ?? []).length > 0
        return (
          <article className={`question ${sensitive ? "sensitive" : ""}`} key={q.question_id ?? idx}>
            <div className="section-heading">
              <div>
                <p className="eyebrow">Question {idx + 1}</p>
                <h3>{q.competency ?? "通用能力"}</h3>
              </div>
              {q.evidence_chunk_ids && q.evidence_chunk_ids.length > 0 ? (
                <span>{q.evidence_chunk_ids.length} 条证据</span>
              ) : null}
            </div>
            <p className="description-text">{q.question_text}</p>
            {q.rationale ? <p className="muted">{q.rationale}</p> : null}
            {sensitive ? <p className="sens">⚠ 触发敏感属性保护：{q.sensitivity_flags!.join("、")}</p> : null}
          </article>
        )
      })}
    </div>
  )
}
