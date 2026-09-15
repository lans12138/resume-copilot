import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { api } from "../api/client"
import type { JobInput, JobStatus } from "../api/types"
import { ErrorNotice, LoadingState } from "../components/Feedback"
import { JobForm } from "../components/JobForm"
import { useAppStore } from "../state/session"

const statusText: Record<JobStatus, string> = { DRAFT: "草稿", ACTIVE: "招聘中", CLOSED: "已关闭" }

export function JobsPage() {
  const token = useAppStore((state) => state.accessToken)!
  const user = useAppStore((state) => state.currentUser)
  const status = useAppStore((state) => state.jobStatusFilter)
  const setStatus = useAppStore((state) => state.setJobStatusFilter)
  const [creating, setCreating] = useState(false)
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const jobs = useQuery({ queryKey: ["jobs", status], queryFn: () => api.listJobs(token, status) })
  const create = useMutation({
    mutationFn: (input: JobInput) => api.createJob(token, input),
    onSuccess: async (job) => { await queryClient.invalidateQueries({ queryKey: ["jobs"] }); navigate(`/jobs/${job.id}`) },
  })

  return <section>
    <div className="page-heading">
      <div><p className="eyebrow">Talent workspace</p><h1>岗位工作台</h1><p className="muted">定义岗位标准，协同招聘负责人，并追踪每次版本变化。</p></div>
      {user?.role === "HR" ? <button className="button button-primary" type="button" onClick={() => setCreating(true)}>＋ 新建岗位</button> : null}
    </div>

    {creating ? <section className="panel form-panel" aria-labelledby="new-job-heading">
      <div className="section-heading"><div><p className="eyebrow">New position</p><h2 id="new-job-heading">创建岗位草稿</h2></div></div>
      {create.error ? <ErrorNotice error={create.error} /> : null}
      <JobForm submitLabel="创建并查看" busy={create.isPending} error={create.error} onSubmit={(input) => create.mutate(input)} onCancel={() => setCreating(false)} />
    </section> : null}

    <div className="toolbar">
      <label htmlFor="status-filter">岗位状态</label>
      <select id="status-filter" value={status} onChange={(event) => setStatus(event.target.value as JobStatus | "ALL")}>
        <option value="ALL">全部</option><option value="DRAFT">草稿</option><option value="ACTIVE">招聘中</option><option value="CLOSED">已关闭</option>
      </select>
      <span>{jobs.data ? `${jobs.data.total} 个岗位` : "正在统计"}</span>
    </div>

    {jobs.isLoading ? <LoadingState label="正在读取岗位" /> : null}
    {jobs.error ? <ErrorNotice error={jobs.error} /> : null}
    {jobs.data?.items.length === 0 ? <div className="empty-state"><strong>还没有符合条件的岗位</strong><span>{user?.role === "HR" ? "创建一个岗位草稿，开始第一条招聘流程。" : "请联系 HR 分配可访问的岗位。"}</span></div> : null}
    <div className="job-grid">
      {jobs.data?.items.map((job) => <Link className="job-card" to={`/jobs/${job.id}`} key={job.id}>
        <div className="job-card-top"><span className={`status-pill status-${job.status.toLowerCase()}`}>{statusText[job.status]}</span><span>v{job.current_version.version_no}</span></div>
        <h2>{job.title}</h2><p>{job.current_version.description}</p>
        <div className="skill-row">{job.current_version.requirements.required_skills.slice(0, 4).map((skill) => <span key={skill}>{skill}</span>)}</div>
        <footer><span>更新于 {new Date(job.updated_at).toLocaleDateString("zh-CN")}</span><strong>查看详情 →</strong></footer>
      </Link>)}
    </div>
  </section>
}
