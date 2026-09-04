import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { api } from "../api/client"
import type { CandidateFilter } from "../api/types"
import { ErrorNotice, LoadingState } from "../components/Feedback"
import { ExplicitFilterBar } from "../components/ExplicitFilterBar"
import { CandidateTable } from "../components/CandidateTable"
import { applyCandidateFilter } from "../lib/candidateFilter"
import { useAppStore } from "../state/session"

export function CandidatesPage() {
  const token = useAppStore((state) => state.accessToken)!
  const hardRule = useAppStore((state) => state.candidateHardRule)
  const channel = useAppStore((state) => state.candidateChannel)
  const jobs = useQuery({ queryKey: ["jobs", "ALL"], queryFn: () => api.listJobs(token, "ALL") })
  const [jobId, setJobId] = useState("")

  const candidates = useQuery({
    queryKey: ["candidates", jobId],
    queryFn: () => api.listCandidates(token, jobId),
    enabled: Boolean(jobId),
  })

  // Explicit filter is presentation-only: it narrows what is shown but never
  // mutates the source ranking (detailed design §15.4 "筛选不改变 MatchRun 范围").
  const filter: CandidateFilter = { hard_rule: hardRule, channel }
  const visible = useMemo(
    () => (candidates.data ? applyCandidateFilter(candidates.data.items, filter) : []),
    [candidates.data, filter],
  )
  const hasFail = useMemo(
    () => (candidates.data ? candidates.data.items.some((item) => item.hard_rule?.overall === "FAIL") : false),
    [candidates.data],
  )

  return (
    <section>
      <div className="page-heading">
        <div>
          <p className="eyebrow">Talent matching</p>
          <h1>候选人检索</h1>
          <p className="muted">按岗位标准对可用候选人库做三路召回与 RRF 融合排名，硬规则结果供筛选参考。</p>
        </div>
      </div>

      <div className="toolbar">
        <label htmlFor="job-picker">选择岗位</label>
        <select id="job-picker" value={jobId} onChange={(event) => setJobId(event.target.value)}>
          <option value="">请选择岗位…</option>
          {jobs.data?.items.map((job) => (
            <option key={job.id} value={job.id}>{job.title}（v{job.current_version.version_no}）</option>
          ))}
        </select>
        {jobs.isLoading ? <span className="muted">正在加载岗位</span> : null}
      </div>

      {jobs.error ? <ErrorNotice error={jobs.error} /> : null}

      {jobId ? <ExplicitFilterBar /> : null}

      {jobId && candidates.isLoading ? <LoadingState label="正在检索候选人" /> : null}
      {jobId && candidates.error ? <ErrorNotice error={candidates.error} /> : null}

      {jobId && candidates.data ? (
        <>
          <p className="muted">
            共 {candidates.data.total} 名候选人进入 Top-{candidates.data.config.top_k}；当前筛选显示 {visible.length} 名。筛选仅影响展示，不改变匹配范围。
          </p>
          {visible.length === 0 ? (
            <div className="empty-state">
              <strong>当前筛选条件下没有候选人</strong>
              <span>
                {hasFail
                  ? "存在未通过硬规则的候选人；清除筛选即可查看全部结果。"
                  : "请调整筛选条件或选择其他岗位。"}
              </span>
            </div>
          ) : (
            <CandidateTable items={visible} />
          )}
        </>
      ) : null}
    </section>
  )
}
