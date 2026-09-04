import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { Link, useParams } from "react-router-dom"
import { ApiError, api } from "../api/client"
import type { DecisionAction } from "../api/types"
import { ActionDiff } from "../components/ActionDiff"
import { DecisionActions } from "../components/DecisionActions"
import { EditableParamsForm } from "../components/EditableParamsForm"
import { ApprovalStatusBadge } from "../components/RunStatusBadge"
import { ErrorNotice, LoadingState } from "../components/Feedback"
import { useAppStore } from "../state/session"

export function ApprovalPage() {
  const { approvalId = "" } = useParams()
  const token = useAppStore((state) => state.accessToken)!
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [editedParams, setEditedParams] = useState<Record<string, unknown> | null>(null)
  const [stale, setStale] = useState(false)
  const [done, setDone] = useState<{ status: string } | null>(null)

  const approvalQuery = useQuery({
    queryKey: ["approval", approvalId],
    queryFn: () => api.getApproval(token, approvalId),
    enabled: Boolean(approvalId),
  })

  const decide = useMutation({
    mutationFn: (input: { decision: DecisionAction; edited_params?: Record<string, unknown> | null }) =>
      api.decideApproval(
        token,
        approvalId,
        {
          decision: input.decision,
          expected_version: approvalQuery.data!.version,
          edited_params: input.edited_params ?? null,
        },
        crypto.randomUUID(),
      ),
    onSuccess: (result) => {
      setDone({ status: result.status })
      setEditing(false)
      setEditedParams(null)
      queryClient.invalidateQueries({ queryKey: ["approval", approvalId] })
      if (result.application_run_id) {
        queryClient.invalidateQueries({ queryKey: ["application-run", result.application_run_id] })
      }
    },
    onError: (err) => {
      if (err instanceof ApiError && (err.status === 409 || err.status === 403)) {
        // 409 (EXPIRED / VERSION_CONFLICT) or 403: re-read current fact and let the
        // user confirm rather than auto-replaying the decision (§15.5).
        setStale(true)
        void queryClient.invalidateQueries({ queryKey: ["approval", approvalId] })
      }
    },
  })

  if (approvalQuery.isLoading) return <LoadingState label="正在读取审批" />
  if (approvalQuery.error) return <section><Link className="back-link" to="/jobs">← 返回岗位列表</Link><ErrorNotice error={approvalQuery.error} /></section>
  const approval = approvalQuery.data
  if (!approval) return null

  const canDecide = approval.status === "PENDING"
  const displayFinal = editedParams ?? approval.final_params
  const decidedAction: DecisionAction = editedParams !== null ? "EDIT" : "APPROVE"

  function onApprove() {
    setStale(false)
    decide.mutate({ decision: decidedAction, edited_params: editedParams })
  }
  function onReject() {
    setStale(false)
    decide.mutate({ decision: "REJECT" })
  }

  return (
    <section>
      <Link className="back-link" to={`/application-runs/${approval.application_run_id}`}>← 返回申请流程</Link>
      <header className="page-heading">
        <div>
          <div className="detail-meta">
            <ApprovalStatusBadge status={approval.status} />
            <span>审批 <code>{approval.id.slice(0, 8)}</code></span>
          </div>
          <h1>审批决策</h1>
          <p>关联申请流程 <code>{approval.application_run_id.slice(0, 8)}</code>{approval.expires_at ? ` · 过期于 ${new Date(approval.expires_at).toLocaleString("zh-CN")}` : ""}</p>
        </div>
      </header>

      {done ? (
        <div className="notice"><strong>决策已提交，当前状态：{done.status}</strong><span><Link className="back-link" to={`/application-runs/${approval.application_run_id}`}>返回申请流程查看结果 →</Link></span></div>
      ) : null}
      {stale ? <div className="notice notice-error"><strong>页面数据已发生变化（可能已过期或版本冲突）</strong><span>已刷新为最新事实，请确认后再决策。</span></div> : null}
      {!canDecide ? <div className="notice"><strong>该审批已不可决策</strong><span>当前状态：{approval.status}。</span></div> : null}
      {decide.error && !stale ? <ErrorNotice error={decide.error} /> : null}

      <div className="run-stack">
        <section className="panel" aria-label="动作差异">
          <div className="section-heading"><div><p className="eyebrow">Action diff</p><h2>原提案与参数</h2></div></div>
          <ActionDiff actionType={approval.action_type} originalParams={approval.original_params} finalParams={displayFinal} />
        </section>

        {canDecide && editing ? (
          <section className="panel form-panel" aria-label="编辑参数">
            <div className="section-heading"><div><p className="eyebrow">Edit params</p><h2>修改参数</h2></div></div>
            <EditableParamsForm initial={displayFinal} onApply={(params) => { setEditedParams(params); setEditing(false) }} />
          </section>
        ) : null}

        {canDecide ? (
          <section className="panel" aria-label="决策">
            <div className="section-heading"><div><p className="eyebrow">Decision</p><h2>决策</h2></div></div>
            <DecisionActions onApprove={onApprove} onEdit={() => setEditing(true)} onReject={onReject} editing={editing} busy={decide.isPending} disabled={decide.isPending} />
          </section>
        ) : null}
      </div>
    </section>
  )
}
