import type { ClaimOut, ReportList } from "../api/types"

const supportLabel: Record<string, string> = {
  SUPPORTED: "支持",
  PARTIAL: "部分支持",
  INSUFFICIENT: "证据不足",
}
const supportTone: Record<string, string> = {
  SUPPORTED: "ok",
  PARTIAL: "warn",
  INSUFFICIENT: "danger",
}
const impactLabel: Record<string, string> = {
  HIGH: "高",
  MEDIUM: "中",
  LOW: "低",
}

function ClaimCard({ claim }: { claim: ClaimOut }) {
  return (
    <article className="claim">
      <div className="claim-head">
        <strong>{claim.claim_text}</strong>
        <span className={`badge badge-${supportTone[claim.support_level] ?? "muted"}`}>
          {supportLabel[claim.support_level] ?? claim.support_level}
        </span>
        <span className="badge badge-neutral">影响：{impactLabel[claim.impact_level] ?? claim.impact_level}</span>
        {claim.confidence_note ? <span className="badge badge-muted">{claim.confidence_note}</span> : null}
      </div>
      {claim.evidences.length === 0 ? (
        <p className="muted">该结论暂无关联证据摘录。</p>
      ) : (
        claim.evidences.map((ev) => (
          <blockquote className="evidence" key={ev.evidence_chunk_id}>
            “{ev.quote_text}”
          </blockquote>
        ))
      )}
    </article>
  )
}

/** Evidence-backed reports for a MatchRun (detailed design §15.4). */
export function ClaimEvidencePanel({ reports }: { reports: ReportList }) {
  if (reports.reports.length === 0) {
    return <p className="muted">该分析流程尚未生成证据化报告。</p>
  }
  return (
    <div>
      {reports.reports.map((report) => (
        <section className="panel" key={report.id} style={{ marginBottom: "1rem" }}>
          <div className="section-heading">
            <div>
              <p className="eyebrow">Candidate report</p>
              <h2>候选人 <code>{report.candidate_profile_id.slice(0, 8)}</code></h2>
            </div>
            <span>综合评分 {report.overall_score.toFixed(2)} · {report.recommendation}</span>
          </div>
          <p className="description-text">{report.summary}</p>
          <h3>结论与证据</h3>
          {report.claims
            .slice()
            .sort((a, b) => a.display_order - b.display_order)
            .map((claim) => (
              <ClaimCard key={`${report.id}-${claim.display_order}`} claim={claim} />
            ))}
        </section>
      ))}
    </div>
  )
}
