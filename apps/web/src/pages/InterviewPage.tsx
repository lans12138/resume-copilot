import { useQuery } from "@tanstack/react-query"
import { Link, useParams } from "react-router-dom"
import { api } from "../api/client"
import { ErrorNotice, LoadingState } from "../components/Feedback"
import { QuestionSet } from "../components/QuestionSet"
import { InterviewStatusBadge } from "../components/RunStatusBadge"
import { ScheduleResult } from "../components/ScheduleResult"
import { useAppStore } from "../state/session"

export function InterviewPage() {
  const { interviewId = "" } = useParams()
  const token = useAppStore((state) => state.accessToken)!
  const interviewQuery = useQuery({
    queryKey: ["interview", interviewId],
    queryFn: () => api.getInterview(token, interviewId),
    enabled: Boolean(interviewId),
  })
  const runQuery = useQuery({
    queryKey: ["application-run", interviewQuery.data?.run_id],
    queryFn: () => api.getApplicationRun(token, interviewQuery.data!.run_id),
    enabled: Boolean(interviewQuery.data?.run_id),
  })

  if (interviewQuery.isLoading) return <LoadingState label="正在读取面试安排" />
  if (interviewQuery.error) return <section><Link className="back-link" to="/jobs">← 返回岗位列表</Link><ErrorNotice error={interviewQuery.error} /></section>
  const interview = interviewQuery.data
  if (!interview) return null

  return (
    <section>
      <Link className="back-link" to={`/application-runs/${interview.run_id}`}>← 返回申请流程</Link>
      <header className="page-heading">
        <div>
          <div className="detail-meta">
            <InterviewStatusBadge status={interview.status} />
            <span>面试安排 <code>{interview.id.slice(0, 8)}</code></span>
          </div>
          <h1>面试安排</h1>
          <p>关联申请 <code>{interview.application_id.slice(0, 8)}</code> · 创建于 {new Date(interview.created_at).toLocaleString("zh-CN")}</p>
        </div>
      </header>

      <div className="run-stack">
        <section className="panel" aria-label="面试问题集">
          <div className="section-heading"><div><p className="eyebrow">Question set</p><h2>面试问题集</h2></div></div>
          {runQuery.isLoading ? <LoadingState label="读取问题集" /> : runQuery.error ? <ErrorNotice error={runQuery.error} /> : <QuestionSet questionSet={runQuery.data?.question_set ?? null} />}
        </section>

        <section className="panel" aria-label="安排详情">
          <div className="section-heading"><div><p className="eyebrow">Schedule</p><h2>安排详情</h2></div></div>
          <ScheduleResult proposal={interview.proposal} status={interview.status} />
        </section>
      </div>
    </section>
  )
}
