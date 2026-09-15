import { useEffect } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useParams } from "react-router-dom"
import { api, ApiError } from "../api/client"
import type { RunStatus } from "../api/types"
import { ErrorNotice, LoadingState } from "../components/Feedback"
import { ClaimEvidencePanel } from "../components/ClaimEvidencePanel"
import { RankingTable } from "../components/RankingTable"
import { RunStatusBadge } from "../components/RunStatusBadge"
import { RunTimeline } from "../components/RunTimeline"
import { useAppStore } from "../state/session"

export function MatchRunPage() {
  const { runId = "" } = useParams()
  const token = useAppStore((state) => state.accessToken)!
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const runQuery = useQuery({
    queryKey: ["match-run", runId],
    queryFn: () => api.getMatchRun(token, runId),
    enabled: Boolean(runId),
    // Convergence backstop (FIN-005): the SSE terminal frame drives an instant
    // refetch via RunTimeline.onTerminal, but if that frame is lost in transit
    // (proxy buffering, dropped connection), poll until the run reaches a terminal
    // status so the candidate ranking always appears. Stops polling once terminal.
    refetchInterval: (query) => {
      const s = query.state.data?.status
      const terminal =
        s === "COMPLETED" || s === "FAILED" || s === "CANCELLED"
      return terminal ? false : 2000
    },
  })
  const reportsQuery = useQuery({
    queryKey: ["match-run", runId, "reports"],
    queryFn: () => api.getReports(token, runId),
    enabled: Boolean(runId),
    // The worker persists evidence-backed reports in the same commit as the
    // COMPLETED run (IMP-020), but this query first fires on mount while the run
    // is still CREATED and caches an empty list. Poll until the run is terminal
    // so the evidence panel converges onto the generated reports (FIN-005
    // backstop). Stops once terminal, and the effect below forces one final
    // refetch at the terminal transition so the panel is never left on a stale
    // empty list if the last poll tick landed before the commit.
    refetchInterval: (query) => {
      const run = queryClient.getQueryData<{ status: string }>(["match-run", runId])
      const s = run?.status
      const terminal = s === "COMPLETED" || s === "FAILED" || s === "CANCELLED"
      return terminal ? false : 2000
    },
  })
  const retry = useMutation({ mutationFn: () => api.retryMatchRun(token, runId), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["match-run", runId] }) })
  const cancel = useMutation({ mutationFn: () => api.cancelMatchRun(token, runId), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["match-run", runId] }) })
  const startApplication = useMutation({
    mutationFn: (applicationId: string) => api.createApplicationRun(
      token,
      applicationId,
      reportsQuery.data?.reports.find((report) => report.application_id === applicationId)?.id,
    ),
    onSuccess: (created) => navigate(`/application-runs/${created.run_id}`),
    onError: (error) => {
      if (error instanceof ApiError && error.code === "APPLICATION_RUN_ALREADY_ACTIVE") {
        const currentRunId = error.details.current_run_id
        if (typeof currentRunId === "string") navigate(`/application-runs/${currentRunId}`)
      }
    },
  })

  // When the run reaches a terminal state the reports may have just been
  // committed; force one refetch so the evidence panel shows them even if the
  // last poll tick landed before the commit (FIN-005 convergence backstop).
  // Declared before any early return so it obeys the Rules of Hooks.
  useEffect(() => {
    const s = runQuery.data?.status
    const t = s === "COMPLETED" || s === "FAILED" || s === "CANCELLED"
    if (t) reportsQuery.refetch()
  }, [runQuery.data?.status, reportsQuery.refetch])

  if (runQuery.isLoading) return <LoadingState label="正在读取分析流程" />
  if (runQuery.error) return <section><Link className="back-link" to="/jobs">← 返回岗位列表</Link><ErrorNotice error={runQuery.error} /></section>
  const run = runQuery.data
  if (!run) return null
  const status = run.status as RunStatus
  const terminal = status === "COMPLETED" || status === "FAILED" || status === "CANCELLED"

  return (
    <section>
      <Link className="back-link" to={`/jobs/${run.job_id}`}>← 返回岗位</Link>
      <header className="page-heading">
        <div>
          <div className="detail-meta">
            <RunStatusBadge status={status} />
            <span>分析流程 <code>{run.run_id.slice(0, 8)}</code></span>
          </div>
          <h1>批量匹配分析</h1>
          <p>岗位版本 v{run.job_version_id.slice(0, 8)} · 规则 {run.rule_version} · 创建于 {new Date(run.created_at).toLocaleString("zh-CN")}</p>
        </div>
        <div className="button-row">
          {status === "FAILED" ? <button className="button button-ghost" type="button" disabled={retry.isPending} onClick={() => retry.mutate()}>重试</button> : null}
          {!terminal ? <button className="button button-danger" type="button" disabled={cancel.isPending} onClick={() => cancel.mutate()}>取消</button> : null}
        </div>
      </header>
      {retry.error ? <ErrorNotice error={retry.error} /> : null}
      {cancel.error ? <ErrorNotice error={cancel.error} /> : null}
      {startApplication.error && !(
        startApplication.error instanceof ApiError &&
        startApplication.error.code === "APPLICATION_RUN_ALREADY_ACTIVE"
      ) ? <ErrorNotice error={startApplication.error} /> : null}

      <div className="run-stack">
        <RunTimeline runId={runId} runType="MATCH" onTerminal={() => queryClient.invalidateQueries({ queryKey: ["match-run", runId] })} />

        <section className="panel" aria-label="候选人排名">
          <div className="section-heading">
            <div><p className="eyebrow">Ranking table</p><h2>候选人排名</h2></div>
            <span>{run.candidates.length} 名</span>
          </div>
          <RankingTable
            candidates={run.candidates}
            onStartApplication={(applicationId) => startApplication.mutate(applicationId)}
            startingApplicationId={startApplication.isPending ? startApplication.variables : undefined}
          />
        </section>

        <section className="panel" aria-label="证据化报告">
          <div className="section-heading">
            <div><p className="eyebrow">Claim & evidence</p><h2>结论与证据</h2></div>
            {reportsQuery.isLoading ? <span>加载中…</span> : null}
          </div>
          {reportsQuery.error ? <ErrorNotice error={reportsQuery.error} /> : <ClaimEvidencePanel reports={reportsQuery.data ?? { reports: [] }} />}
        </section>
      </div>
    </section>
  )
}
