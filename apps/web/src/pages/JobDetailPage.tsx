import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState, type FormEvent } from "react"
import { Link, useNavigate, useParams } from "react-router-dom"
import { api } from "../api/client"
import type { JobInput, JobStatus } from "../api/types"
import { ErrorNotice, LoadingState } from "../components/Feedback"
import { JobForm } from "../components/JobForm"
import { RunStatusBadge } from "../components/RunStatusBadge"
import { useAppStore } from "../state/session"

const statusText: Record<JobStatus, string> = { DRAFT: "草稿", ACTIVE: "招聘中", CLOSED: "已关闭" }

export function JobDetailPage() {
  const { jobId = "" } = useParams()
  const token = useAppStore((state) => state.accessToken)!
  const user = useAppStore((state) => state.currentUser)
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const [assigneeId, setAssigneeId] = useState("")
  const queryClient = useQueryClient()
  const matchRuns = useQuery({ queryKey: ["match-runs", jobId], queryFn: () => api.listMatchRuns(token, jobId), enabled: Boolean(jobId) })
  const applications = useQuery({ queryKey: ["job-applications", jobId], queryFn: () => api.listJobApplications(token, jobId), enabled: Boolean(jobId) })
  const launch = useMutation({ mutationFn: () => api.createMatchRun(token, jobId, {}), onSuccess: (res) => navigate(`/match-runs/${res.run_id}`) })
  const jobQuery = useQuery({ queryKey: ["job", jobId], queryFn: () => api.getJob(token, jobId), enabled: Boolean(jobId) })
  const assignments = useQuery({ queryKey: ["job", jobId, "assignments"], queryFn: () => api.listAssignments(token, jobId), enabled: Boolean(jobId) })
  const refresh = async () => {
    await Promise.all([queryClient.invalidateQueries({ queryKey: ["job", jobId] }), queryClient.invalidateQueries({ queryKey: ["jobs"] })])
  }
  const update = useMutation({ mutationFn: (input: JobInput) => api.updateJob(token, jobId, jobQuery.data!.version, input), onSuccess: async () => { setEditing(false); await refresh() } })
  const transition = useMutation({ mutationFn: (action: "activate" | "close") => api.transitionJob(token, jobId, action, jobQuery.data!.version), onSuccess: refresh })
  const grant = useMutation({ mutationFn: (userId: string) => api.grantAssignment(token, jobId, userId), onSuccess: async () => { setAssigneeId(""); await queryClient.invalidateQueries({ queryKey: ["job", jobId, "assignments"] }) } })
  const revoke = useMutation({ mutationFn: (userId: string) => api.revokeAssignment(token, jobId, userId), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["job", jobId, "assignments"] }) })

  if (jobQuery.isLoading) return <LoadingState label="正在读取岗位详情" />
  if (jobQuery.error) return <section><Link className="back-link" to="/jobs">← 返回岗位列表</Link><ErrorNotice error={jobQuery.error} /></section>
  const job = jobQuery.data
  if (!job) return <div className="empty-state"><strong>岗位没有可用版本</strong><span>请返回列表重新创建岗位。</span></div>
  const requirements = job.current_version.requirements
  function submitAssignment(event: FormEvent<HTMLFormElement>) { event.preventDefault(); grant.mutate(assigneeId.trim()) }

  return <section>
    <Link className="back-link" to="/jobs">← 返回岗位列表</Link>
    <header className="detail-header">
      <div><div className="detail-meta"><span className={`status-pill status-${job.status.toLowerCase()}`}>{statusText[job.status]}</span><span>当前版本 v{job.current_version.version_no}</span></div><h1>{job.title}</h1><p>最后更新于 {new Date(job.updated_at).toLocaleString("zh-CN")}</p></div>
      {user?.role === "HR" ? <div className="button-row">
        {job.status !== "CLOSED" ? <button className="button button-ghost" type="button" onClick={() => setEditing(true)}>编辑版本</button> : null}
        {job.status === "DRAFT" ? <button className="button button-primary" type="button" disabled={transition.isPending} onClick={() => transition.mutate("activate")}>启用岗位</button> : null}
        {job.status === "ACTIVE" ? <button className="button button-danger" type="button" disabled={transition.isPending} onClick={() => transition.mutate("close")}>关闭岗位</button> : null}
      </div> : null}
    </header>
    {transition.error ? <ErrorNotice error={transition.error} /> : null}

    {editing ? <section className="panel form-panel" aria-labelledby="edit-heading"><div className="section-heading"><h2 id="edit-heading">编辑岗位版本</h2></div>{update.error ? <ErrorNotice error={update.error} /> : null}<JobForm initial={job} submitLabel="保存新版本" busy={update.isPending} error={update.error} onSubmit={(input) => update.mutate(input)} onCancel={() => setEditing(false)} /></section> : null}

    <div className="detail-grid">
      <section className="panel content-panel"><div className="section-heading"><div><p className="eyebrow">Version editor</p><h2>岗位说明</h2></div><span>v{job.current_version.version_no}</span></div><p className="description-text">{job.current_version.description}</p><h3>人才要求</h3><dl className="requirement-list"><div><dt>必备技能</dt><dd>{requirements.required_skills.join("、") || "暂未设置"}</dd></div><div><dt>加分技能</dt><dd>{requirements.preferred_skills.join("、") || "暂未设置"}</dd></div><div><dt>最低经验</dt><dd>{requirements.minimum_years_experience === null ? "不限" : `${requirements.minimum_years_experience} 年`}</dd></div><div><dt>学历要求</dt><dd>{requirements.education_level || "不限"}</dd></div></dl>{job.status === "CLOSED" ? <div className="notice"><strong>该岗位已关闭</strong><span>版本内容已锁定，仅供历史查看。</span></div> : null}</section>

      <section className="panel"><div className="section-heading"><div><p className="eyebrow">Assignment table</p><h2>负责人分配</h2></div><span>{assignments.data?.total ?? 0} 条记录</span></div>{assignments.error ? <ErrorNotice error={assignments.error} /> : null}{user?.role === "HR" ? <form className="inline-form" onSubmit={submitAssignment}><label htmlFor="assignee-id">招聘主管用户 ID</label><div><input id="assignee-id" value={assigneeId} onChange={(event) => setAssigneeId(event.target.value)} required placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" /><button className="button button-primary" disabled={grant.isPending}>分配</button></div></form> : null}{grant.error ? <ErrorNotice error={grant.error} /> : null}{revoke.error ? <ErrorNotice error={revoke.error} /> : null}<div className="table-wrap"><table><thead><tr><th>用户</th><th>状态</th><th>分配时间</th>{user?.role === "HR" ? <th>操作</th> : null}</tr></thead><tbody>{assignments.data?.items.map((item) => <tr key={item.id}><td><code>{item.user_id}</code></td><td>{item.revoked_at ? "已撤销" : "有效"}</td><td>{new Date(item.assigned_at).toLocaleDateString("zh-CN")}</td>{user?.role === "HR" ? <td>{item.revoked_at ? "—" : <button className="text-button" type="button" onClick={() => revoke.mutate(item.user_id)}>撤销</button>}</td> : null}</tr>)}{assignments.data?.items.length === 0 ? <tr><td colSpan={4}>尚未分配招聘主管</td></tr> : null}</tbody></table></div></section>

      <section className="panel applications-panel"><div className="section-heading"><div><p className="eyebrow">Run table</p><h2>分析流程与申请</h2></div><span>IMP-026 闭环</span></div>
        {user?.role === "HR" && job.status === "ACTIVE" ? <div className="button-row"><button className="button button-primary" type="button" disabled={launch.isPending} onClick={() => launch.mutate()}>启动批量分析</button></div> : null}
        {launch.error ? <ErrorNotice error={launch.error} /> : null}

        <h3>批量匹配分析</h3>
        <div className="table-wrap">
          <table><thead><tr><th>流程</th><th>状态</th><th>创建</th></tr></thead>
          <tbody>
            {(matchRuns.data?.runs ?? []).map((r) => <tr key={r.run_id}><td><Link className="text-button" to={`/match-runs/${r.run_id}`}>{r.run_id.slice(0, 8)}</Link></td><td><RunStatusBadge status={r.status} /></td><td>{new Date(r.created_at).toLocaleDateString("zh-CN")}</td></tr>)}
            {(matchRuns.data?.runs ?? []).length === 0 ? <tr><td colSpan={3}>尚未运行分析</td></tr> : null}
          </tbody></table>
        </div>

        <h3>单人申请流程</h3>
        <div className="table-wrap">
          <table><thead><tr><th>流程</th><th>状态</th><th>报告</th></tr></thead>
          <tbody>
            {(applications.data ?? []).map((a) => <tr key={a.run_id}><td><Link className="text-button" to={`/application-runs/${a.run_id}`}>{a.run_id.slice(0, 8)}</Link></td><td><RunStatusBadge status={a.status} /></td><td>{a.match_report_id ? <code>{a.match_report_id.slice(0, 8)}</code> : "—"}</td></tr>)}
            {(applications.data?.length ?? 0) === 0 ? <tr><td colSpan={3}>暂无申请流程</td></tr> : null}
          </tbody></table>
        </div>
      </section>
    </div>
  </section>
}
