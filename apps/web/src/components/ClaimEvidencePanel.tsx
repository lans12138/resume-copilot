import { Link } from "react-router-dom"
import type { ClaimOut, ReportList } from "../api/types"
import { candidateName, shortProfileId } from "../lib/candidateDisplay"
import { describeBlock, parseLocator } from "../lib/locator"

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

// PORT-003: a report mixes two kinds of statement and they must never read alike.
// A RULE claim is a deterministic verdict over confirmed profile fields (BR-002);
// a MODEL claim is commentary whose citations the server verified but whose
// judgement carries no authority (BR-001). Naming the source is the whole point —
// a reader who cannot tell them apart will read the model's wording as the verdict.
const sourceLabel: Record<string, string> = {
  RULE: "规则判定",
  MODEL: "模型解释",
}
const sourceTone: Record<string, string> = {
  RULE: "neutral",
  MODEL: "muted",
}

function ClaimCard({ claim, documentId }: { claim: ClaimOut; documentId: string | null }) {
  return (
    <article className="claim">
      <div className="claim-head">
        <strong>{claim.claim_text}</strong>
        <span className={`badge badge-${sourceTone[claim.source] ?? "muted"}`}>
          {sourceLabel[claim.source] ?? claim.source}
        </span>
        <span className={`badge badge-${supportTone[claim.support_level] ?? "muted"}`}>
          {supportLabel[claim.support_level] ?? claim.support_level}
        </span>
        <span className="badge badge-neutral">影响：{impactLabel[claim.impact_level] ?? claim.impact_level}</span>
        {claim.confidence_note ? <span className="badge badge-muted">{claim.confidence_note}</span> : null}
      </div>
      {claim.source === "MODEL" ? (
        // Say the ceiling out loud. A verified citation proves the quote is real,
        // not that it means what the model says it means (§9.4), so a model claim
        // can never be SUPPORTED and a reader has to be told why.
        <p className="muted">
          模型解释：引用已通过服务端校验，但语义支持未经人工评测认定，因此不会显示为“支持”。
        </p>
      ) : null}
      {claim.evidences.length === 0 ? (
        // PORT-002: a claim with no excerpt is a downgrade, not a rendering gap —
        // say so, or "部分支持" next to an empty list reads like a missing row.
        <p className="muted">
          未在候选人原文中定位到支持该结论的片段，支持等级已相应下调。
        </p>
      ) : (
        claim.evidences.map((ev) => (
          <blockquote className="evidence" key={`${ev.evidence_chunk_id}-${ev.quote_start}`}>
            {/* PORT-005: every excerpt says where it came from and can be opened
                there. Reading a quote and then hunting for it across the documents
                list was the "手工切换多页" the roadmap calls out. The link carries
                the chunk id, so the review screen opens already located. */}
            <span className="evidence-quote">“{ev.quote_text}”</span>
            <span className="evidence-source">
              <small>{describeBlock(parseLocator(ev.locator_json))}</small>
              {documentId ? (
                <Link
                  className="text-button"
                  to={`/documents/${documentId}/review?chunk=${ev.evidence_chunk_id}`}
                >
                  查看原文 →
                </Link>
              ) : (
                // No document to open: the profile could not be read, so say why the
                // action is missing rather than dropping it silently.
                <small>来源文档不可用，无法跳转原文</small>
              )}
            </span>
          </blockquote>
        ))
      )}
    </article>
  )
}

/** A one-line statement of where a report's conclusions came from (PORT-005). */
function provenance(claims: ClaimOut[]): string {
  const rule = claims.filter((claim) => claim.source === "RULE").length
  const model = claims.filter((claim) => claim.source === "MODEL").length
  const parts: string[] = []
  if (rule > 0) parts.push(`规则判定 ${rule} 条`)
  if (model > 0) parts.push(`模型解释 ${model} 条`)
  if (parts.length === 0) return "本报告没有结论。"
  return `结果来源：${parts.join(" · ")}。`
}

/** Evidence-backed reports for a MatchRun (detailed design §15.4). */
export function ClaimEvidencePanel({ reports }: { reports: ReportList }) {
  if (reports.reports.length === 0) {
    return (
      <div className="empty-state">
        <strong>该分析流程尚未生成证据化报告</strong>
        <span>
          报告在候选人评分完成后与流程状态一起提交。若流程已结束仍没有报告，请确认流程是否失败，或在候选人排名中查看各候选人的处理状态。
        </span>
      </div>
    )
  }
  return (
    <div>
      {reports.reports.map((report) => (
        <section className="panel" key={report.id} style={{ marginBottom: "1rem" }}>
          <div className="section-heading">
            <div>
              <p className="eyebrow">Candidate report</p>
              {/* PORT-005: the report is organised by candidate, so it is titled by
                  name. The profile id stays as a labelled tracking detail — it is what
                  the API and support use, but it is no longer how a reader identifies
                  the person. */}
              <h2>候选人 {candidateName(report.display_name)}</h2>
              <p className="muted">
                辅助追踪 <code>{shortProfileId(report.candidate_profile_id)}</code>
              </p>
            </div>
            {/* PORT-002: name the score for what it is — a ranking conversion, not
                a model confidence. The backend summary spells this out too. */}
            <span>
              检索排序换算分 {report.overall_score.toFixed(2)}（非模型置信度） · {report.recommendation}
            </span>
          </div>
          <p className="description-text">{report.summary}</p>
          {/* PORT-005: 「标明结果来源」 — each claim already carries its own label, and
              this says the mix up front so a reader knows before scrolling whether any
              of the report is model commentary. */}
          <p className="muted">{provenance(report.claims)}</p>
          <h3>结论与证据</h3>
          {report.claims
            .slice()
            .sort((a, b) => a.display_order - b.display_order)
            .map((claim) => (
              <ClaimCard
                key={`${report.id}-${claim.display_order}`}
                claim={claim}
                documentId={report.document_id}
              />
            ))}
        </section>
      ))}
    </div>
  )
}
