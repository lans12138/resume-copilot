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
        // PORT-002: a claim with no excerpt is a downgrade, not a rendering gap —
        // say so, or "部分支持" next to an empty list reads like a missing row.
        <p className="muted">
          未在候选人原文中定位到支持该结论的片段，支持等级已相应下调。
        </p>
      ) : (
        claim.evidences.map((ev) => (
          <blockquote className="evidence" key={`${ev.evidence_chunk_id}-${ev.quote_start}`}>
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
            {/* PORT-002: name the score for what it is — a ranking conversion, not
                a model confidence. The backend summary spells this out too. */}
            <span>
              检索排序换算分 {report.overall_score.toFixed(2)}（非模型置信度） · {report.recommendation}
            </span>
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
