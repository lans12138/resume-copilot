import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useRef, useState } from "react"
import { Link, useParams } from "react-router-dom"
import { ApiError, api } from "../api/client"
import type { ApprovalStatus, DecisionAction } from "../api/types"
import { ActionDiff } from "../components/ActionDiff"
import { DecisionActions } from "../components/DecisionActions"
import { EditableParamsForm } from "../components/EditableParamsForm"
import { ApprovalStatusBadge } from "../components/RunStatusBadge"
import { ErrorNotice, LoadingState } from "../components/Feedback"
import {
  actionLabel,
  awaitingExecution,
  describeAction,
  describeOutcome,
  isDecidable,
} from "../lib/approval"
import { useAppStore } from "../state/session"

export function ApprovalPage() {
  const { approvalId = "" } = useParams()
  const token = useAppStore((state) => state.accessToken)!
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [editedParams, setEditedParams] = useState<Record<string, unknown> | null>(null)
  const [stale, setStale] = useState(false)
  const [done, setDone] = useState<{ status: ApprovalStatus } | null>(null)
  // Stable Idempotency-Key for the current logical submit. Generated once per user
  // click (not per render / per network retry) so a retried request replays the
  // first response instead of being treated as a new action (FIN-001 §20.3).
  const idempotencyKeyRef = useRef<string>("")

  const approvalQuery = useQuery({
    queryKey: ["approval", approvalId],
    queryFn: () => api.getApproval(token, approvalId),
    enabled: Boolean(approvalId),
    // PORT-005: APPROVED/EDITED means the decision is recorded but the side effect has
    // not been applied yet — execution is a separate step. Poll until it lands, so the
    // page converges onto EXECUTED / EXECUTION_FAILED instead of leaving the reader
    // believing a write happened when the executor had not run.
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status !== undefined && awaitingExecution(status) ? 1000 : false
    },
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
        idempotencyKeyRef.current || crypto.randomUUID(),
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

  const canDecide = isDecidable(approval.status)
  const displayFinal = editedParams ?? approval.final_params
  const decidedAction: DecisionAction = editedParams !== null ? "EDIT" : "APPROVE"
  // The outcome is read from the freshest fact on screen, not from the last submit:
  // once the poll converges onto EXECUTED the page must say so, not keep showing the
  // APPROVED it saw right after the click.
  const outcome = describeOutcome(approval.status)

  function onApprove() {
    setStale(false)
    idempotencyKeyRef.current = crypto.randomUUID()
    decide.mutate({ decision: decidedAction, edited_params: editedParams })
  }
  function onReject() {
    setStale(false)
    idempotencyKeyRef.current = crypto.randomUUID()
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
          <h1>{actionLabel(approval.action_type)}</h1>
          <p>
            关联申请流程 <code>{approval.application_run_id.slice(0, 8)}</code>
            {approval.expires_at ? ` · 过期于 ${new Date(approval.expires_at).toLocaleString("zh-CN")}` : ""}
          </p>
        </div>
      </header>

      {/* The four things a person authorising a write has to know, in the order they
          need them: what will be applied, what I changed, what version I am deciding
          against, and what happened. */}
      <div className="run-stack">
        <section className="panel" aria-label="拟执行动作">
          <div className="section-heading">
            <div><p className="eyebrow">Proposed action</p><h2>拟执行动作</h2></div>
            <span className="badge badge-neutral">{actionLabel(approval.action_type)}</span>
          </div>
          <p className="action-summary">
            {describeAction(approval.action_type, displayFinal ?? approval.original_params)}
          </p>
          {approval.final_params !== null ? (
            <p className="muted">参数已被人工修改，将按修改后的值执行。</p>
          ) : null}
        </section>

        <section className="panel" aria-label="动作差异">
          <div className="section-heading"><div><p className="eyebrow">Action diff</p><h2>原提案与参数</h2></div></div>
          <ActionDiff originalParams={approval.original_params} finalParams={displayFinal} />
        </section>

        <section className="panel" aria-label="当前版本">
          <div className="section-heading">
            <div><p className="eyebrow">Versions</p><h2>当前版本</h2></div>
          </div>
          <dl className="kv">
            <dt>审批版本</dt>
            <dd><code>v{approval.version}</code></dd>
            <dt>决策依据的申请版本</dt>
            <dd>
              {approval.expected_application_version === null
                ? <span className="muted">未记录</span>
                : <code>v{approval.expected_application_version}</code>}
            </dd>
            <dt>幂等键</dt>
            <dd><code>{approval.idempotency_key}</code></dd>
          </dl>
          <p className="muted">
            决策按当前审批版本提交：若同一审批已被他人决策，提交会被拒绝而不是覆盖。
            执行时还会比对申请版本，申请在此期间被改动则执行被拒绝，避免覆盖并发修改。
          </p>
        </section>

        <section className="panel" aria-label="执行结果">
          <div className="section-heading">
            <div><p className="eyebrow">Result</p><h2>执行结果</h2></div>
          </div>
          {/* The header already carries the coarse status badge; this panel says the
              more specific thing — whether the write has happened yet. */}
          <p className="action-summary"><strong>{outcome.label}</strong></p>
          <p className="muted">{outcome.detail}</p>
          {approval.decided_at ? (
            <p className="muted">
              决策于 {new Date(approval.decided_at).toLocaleString("zh-CN", { hour12: false })}
              {approval.decided_by ? ` · 决策人 ${approval.decided_by.slice(0, 8)}` : ""}
            </p>
          ) : null}
          {outcome.guidance ? (
            <div className="notice" role="status">
              <strong>下一步</strong>
              <span>{outcome.guidance}</span>
              {approval.status === "EXECUTION_FAILED" ? (
                <Link className="back-link" to={`/application-runs/${approval.application_run_id}`}>
                  前往申请流程重试 →
                </Link>
              ) : null}
            </div>
          ) : null}
        </section>

        {done ? (
          <div className="notice" role="status">
            <strong>决策已提交，当前状态：{describeOutcome(done.status).label}</strong>
            <span>
              决策已经记录，副作用由执行器应用；本页会自动刷新到最终执行结果。
            </span>
            <Link className="back-link" to={`/application-runs/${approval.application_run_id}`}>返回申请流程查看结果 →</Link>
          </div>
        ) : null}
        {stale ? <div className="notice notice-error"><strong>页面数据已发生变化（可能已过期或版本冲突）</strong><span>已刷新为最新事实，请确认后再决策。</span></div> : null}
        {!canDecide ? <div className="notice"><strong>该审批已不可决策</strong><span>当前状态：{outcome.label}。{outcome.detail}</span></div> : null}
        {decide.error && !stale ? <ErrorNotice error={decide.error} /> : null}

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
