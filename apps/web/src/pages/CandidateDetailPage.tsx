import { Link, useParams, useSearchParams } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import { api } from "../api/client"
import type { HardRuleId, HardRuleResult } from "../api/types"
import { EvidencePreview } from "../components/EvidencePreview"
import { ErrorNotice, LoadingState } from "../components/Feedback"
import { HardRuleBadge } from "../components/HardRuleBadge"
import { PROFILE_STATUS_LABELS } from "../lib/profileEdit"
import { useAppStore } from "../state/session"

const RULE_LABELS: Record<HardRuleId, string> = {
  years_experience: "工作年限",
  required_education: "学历要求",
  required_skills: "必备技能",
}

function channelCell(rank: number | null, score: number | null): string {
  if (rank === null || score === null) return "—"
  return `#${rank} · ${score.toFixed(3)}`
}

function RuleRow({ rule }: { rule: HardRuleResult }) {
  return (
    <tr>
      <td>{RULE_LABELS[rule.rule_id] ?? rule.rule_id}</td>
      <td><HardRuleBadge outcome={rule.result} /></td>
      <td><code>{String(rule.observed_value ?? "—")}</code></td>
      <td><code>{String(rule.required_value ?? "—")}</code></td>
      <td><small>{rule.reason_code}</small></td>
    </tr>
  )
}

/**
 * Single candidate view (FIN-004).
 *
 * Everything here is job-scoped on purpose: the profile and its evidence are only
 * meaningful against a job's requirements, so the page carries the job in the URL
 * and can be deep-linked from the ranking table. The ranking row is shown when the
 * candidate is inside the job's Top-K; when it is not, the page says so instead of
 * pretending the candidate was never evaluated.
 */
export function CandidateDetailPage() {
  const { profileId = "" } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const token = useAppStore((state) => state.accessToken)!
  const jobId = searchParams.get("job") ?? ""

  const jobs = useQuery({ queryKey: ["jobs", "ALL"], queryFn: () => api.listJobs(token, "ALL") })
  const profile = useQuery({
    queryKey: ["job-profile", jobId, profileId],
    queryFn: () => api.getJobProfile(token, jobId, profileId),
    enabled: Boolean(jobId && profileId),
  })
  const evidence = useQuery({
    queryKey: ["job-profile-evidence", jobId, profileId],
    queryFn: () => api.listJobProfileEvidence(token, jobId, profileId),
    enabled: Boolean(jobId && profileId),
  })
  const ranking = useQuery({
    queryKey: ["candidates", jobId],
    queryFn: () => api.listCandidates(token, jobId),
    enabled: Boolean(jobId),
  })

  const row = ranking.data?.items.find((item) => item.candidate_profile_id === profileId) ?? null
  const name = row?.display_name
    || (typeof profile.data?.profile_json["full_name"] === "string" ? profile.data.profile_json["full_name"] : "")
    || "候选人"

  return (
    <section>
      <Link className="back-link" to="/candidates">← 返回候选人检索</Link>

      <div className="detail-header">
        <div>
          <p className="eyebrow">Candidate</p>
          <h1>{name}</h1>
          <p className="muted">
            查看候选人已确认的资料、硬规则明细与证据出处。资料与证据都按岗位读取，切换岗位会改变结论。
          </p>
        </div>
        <div className="detail-meta">
          {profile.data ? (
            <span className="badge badge-neutral">资料 {PROFILE_STATUS_LABELS[profile.data.status]}</span>
          ) : null}
          {row ? <span className="badge badge-info">RRF {row.rrf_score.toFixed(4)}</span> : null}
        </div>
      </div>

      <div className="toolbar">
        <label htmlFor="candidate-job">岗位</label>
        <select
          id="candidate-job"
          value={jobId}
          onChange={(event) => {
            const value = event.target.value
            setSearchParams(value ? { job: value } : {})
          }}
        >
          <option value="">请选择岗位…</option>
          {jobs.data?.items.map((job) => (
            <option key={job.id} value={job.id}>{job.title}</option>
          ))}
        </select>
        <span>资料与证据的读取都需要岗位级授权。</span>
      </div>

      {jobs.error ? <ErrorNotice error={jobs.error} /> : null}
      {!jobId ? (
        <div className="notice" role="status">
          <strong>请选择岗位</strong>
          <span>候选人的资料与证据按岗位读取，选定后即可查看硬规则与原文出处。</span>
        </div>
      ) : null}

      {jobId && profile.isLoading ? <LoadingState label="正在加载候选人资料" /> : null}
      {profile.error ? <ErrorNotice error={profile.error} /> : null}
      {ranking.error ? <ErrorNotice error={ranking.error} /> : null}
      {evidence.error ? <ErrorNotice error={evidence.error} /> : null}

      {profile.data ? (
        <div className="detail-grid">
          <section className="panel content-panel" aria-label="候选人资料">
            <div className="section-heading">
              <h2>资料</h2>
              <span>第 {profile.data.version_no} 版 · schema {profile.data.schema_version}</span>
            </div>
            <dl className="kv">
              <dt>姓名</dt>
              <dd>{name}</dd>
              <dt>工作年限</dt>
              <dd>{profile.data.years_experience ?? "未判定"}</dd>
              <dt>学历</dt>
              <dd>{profile.data.education_level ?? "未判定"}</dd>
              <dt>技能</dt>
              <dd>{profile.data.normalized_skills.join("、") || "—"}</dd>
              <dt>确认时间</dt>
              <dd>
                {profile.data.confirmed_at
                  ? new Date(profile.data.confirmed_at).toLocaleString("zh-CN", { hour12: false })
                  : "尚未确认"}
              </dd>
            </dl>

            {row ? (
              <table className="ranking-table">
                <caption className="muted">该岗位三路召回与融合排名</caption>
                <thead>
                  <tr>
                    <th scope="col">结构化</th>
                    <th scope="col">关键词</th>
                    <th scope="col">向量</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>{channelCell(row.structured_rank, row.structured_score)}</td>
                    <td>{channelCell(row.keyword_rank, row.keyword_score)}</td>
                    <td>{channelCell(row.vector_rank, row.vector_score)}</td>
                  </tr>
                </tbody>
              </table>
            ) : (
              <p className="muted">该候选人不在当前岗位 Top-K 召回结果中，因此没有排名明细。</p>
            )}

            <div className="button-row">
              <Link className="button button-ghost" to={`/documents/${profile.data.document_id}/review`}>
                查看原文与证据 →
              </Link>
            </div>
          </section>

          <section className="panel applications-panel" aria-label="硬规则明细">
            <div className="section-heading">
              <h2>硬规则</h2>
              <HardRuleBadge outcome={row?.hard_rule?.overall ?? null} />
            </div>
            {row?.hard_rule ? (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">规则</th>
                      <th scope="col">结论</th>
                      <th scope="col">实际</th>
                      <th scope="col">要求</th>
                      <th scope="col">原因</th>
                    </tr>
                  </thead>
                  <tbody>
                    {row.hard_rule.rules.map((rule) => <RuleRow key={rule.rule_id} rule={rule} />)}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="muted">没有可用的硬规则结论。</p>
            )}
          </section>
        </div>
      ) : null}

      {profile.data ? (
        <section className="panel" aria-label="证据">
          <div className="section-heading">
            <h2>证据原文</h2>
            <span>{evidence.data ? `${evidence.data.length} 条` : "—"}</span>
          </div>
          {evidence.isLoading ? <LoadingState label="正在加载证据" /> : null}
          <EvidencePreview chunks={evidence.data ?? []} activeLocator={null} />
        </section>
      ) : null}
    </section>
  )
}
