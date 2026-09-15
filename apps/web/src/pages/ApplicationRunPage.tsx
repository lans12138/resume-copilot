import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useParams } from "react-router-dom"
import { api } from "../api/client"
import type { RunStatus } from "../api/types"
import { ErrorNotice, LoadingState } from "../components/Feedback"
import { CurrentApprovalCard } from "../components/CurrentApprovalCard"
import { QuestionSet } from "../components/QuestionSet"
import { InterviewStatusBadge, RunStatusBadge } from "../components/RunStatusBadge"
import { RunTimeline } from "../components/RunTimeline"
import { useAppStore } from "../state/session"

export function ApplicationRunPage() {
  const { runId = "" } = useParams()
  const token = useAppStore((state) => state.accessToken)!
  const queryClient = useQueryClient()
  const runQuery = useQuery({
    queryKey: ["application-run", runId],
    queryFn: () => api.getApplicationRun(token, runId),
    enabled: Boolean(runId),
    // START/RESUME execute in Celery (FIN-005). SSE renders the audit timeline,
    // while this bounded poll converges the aggregate read model at each worker
    // boundary (CREATED/RUNNING -> WAITING_APPROVAL/terminal).
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === "CREATED" || status === "RUNNING" ? 500 : false
    },
  })
  const retry = useMutation({ mutationFn: () => api.retryApplicationRun(token, runId), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["application-run", runId] }) })
  const cancel = useMutation({ mutationFn: () => api.cancelApplicationRun(token, runId), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["application-run", runId] }) })

  if (runQuery.isLoading) return <LoadingState label="正在读取申请流程" />
  if (runQuery.error) return <section><Link className="back-link" to="/jobs">← 返回岗位列表</Link><ErrorNotice error={runQuery.error} /></section>
  const run = runQuery.data
  if (!run) return null
  const status = run.status as RunStatus
  const terminal = status === "COMPLETED" || status === "FAILED" || status === "CANCELLED"

  return (
    <section>
      <Link className="back-link" to={`/jobs`}>← 返回岗位列表</Link>
      <header className="page-heading">
        <div>
          <div className="detail-meta">
            <RunStatusBadge status={status} />
            <span>申请流程 <code>{run.run_id.slice(0, 8)}</code></span>
          </div>
          <h1>单人招聘流程</h1>
          <p>申请 <code>{run.application_id.slice(0, 8)}</code> · 尝试 {run.attempt}{run.completion_reason ? ` · ${run.completion_reason}` : ""}</p>
        </div>
        <div className="button-row">
          {status === "FAILED" ? <button className="button button-ghost" type="button" disabled={retry.isPending} onClick={() => retry.mutate()}>重试</button> : null}
          {!terminal ? <button className="button button-danger" type="button" disabled={cancel.isPending} onClick={() => cancel.mutate()}>取消</button> : null}
        </div>
      </header>
      {retry.error ? <ErrorNotice error={retry.error} /> : null}
      {cancel.error ? <ErrorNotice error={cancel.error} /> : null}

      <div className="run-stack">
        <RunTimeline runId={runId} runType="APPLICATION" onTerminal={() => queryClient.invalidateQueries({ queryKey: ["application-run", runId] })} />

        <section className="panel" aria-label="当前审批">
          <div className="section-heading"><div><p className="eyebrow">Approval</p><h2>当前审批</h2></div></div>
          <CurrentApprovalCard approval={run.current_approval} />
        </section>

        <section className="panel" aria-label="面试问题集">
          <div className="section-heading"><div><p className="eyebrow">Question set</p><h2>面试问题集</h2></div></div>
          <QuestionSet questionSet={run.question_set} />
        </section>

        <section className="panel" aria-label="面试安排">
          <div className="section-heading">
            <div><p className="eyebrow">Schedule</p><h2>面试安排</h2></div>
            {run.interview_id ? <Link className="text-button" to={`/interviews/${run.interview_id}`}>查看面试 →</Link> : null}
          </div>
          {run.interview_id ? (
            <div className="button-row" style={{ marginTop: 0 }}>
              {run.interview_status ? <InterviewStatusBadge status={run.interview_status} /> : null}
              <span className="badge badge-muted">外部编号 {run.interview_external_id?.slice(0, 8)}</span>
            </div>
          ) : (
            <p className="muted">尚未创建面试安排。</p>
          )}
        </section>
      </div>
    </section>
  )
}
