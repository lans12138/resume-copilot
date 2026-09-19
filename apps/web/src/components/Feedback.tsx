import { ApiError } from "../api/client"

/**
 * What went wrong, and what to do about it (PORT-005).
 *
 * The notice used to name the class of failure and stop. "页面数据已发生变化，请刷新后重试"
 * tells a reader the request lost a race but not whether their input survived, and a 503
 * leaves them refreshing a service that is still restarting. Each status now carries the
 * next step explicitly, because the recovery is not the same for a permission problem, a
 * lost race and an outage — and the reader cannot infer which one they have.
 */
const statusRecovery: Record<number, { lead: string; next: string }> = {
  0: {
    lead: "无法连接服务。",
    next: "请检查网络连接，确认服务已启动，然后重试。",
  },
  401: {
    lead: "登录状态已失效。",
    next: "请重新登录后再试；本次操作没有被执行。",
  },
  403: {
    lead: "当前账号没有执行此操作的权限。",
    next: "请联系岗位负责人把该岗位指派给你，或改用有权限的账号。",
  },
  404: {
    lead: "请求的资源不存在。",
    next: "它可能已被删除或从未创建；返回上一页确认后再试。",
  },
  409: {
    lead: "页面数据已发生变化，请刷新后重试。",
    next: "页面已刷新为最新事实，请确认后再提交，避免覆盖他人的修改。",
  },
  422: {
    lead: "提交内容未通过校验，请检查各字段。",
    next: "按字段旁的提示修正后重新提交。",
  },
  429: {
    lead: "请求过于频繁。",
    next: "稍等几秒后重试。",
  },
  503: {
    lead: "核心依赖暂时不可用，请稍后重试。",
    next: "数据库、缓存或对象存储可能正在重启；稍后重试，若持续失败请查看服务状态。",
  },
}

export function ErrorNotice({ error }: { error: unknown }) {
  const apiError = error instanceof ApiError ? error : null
  const recovery = apiError ? statusRecovery[apiError.status] : undefined
  const lead = recovery?.lead ?? apiError?.message ?? "出现未知错误，请重试。"
  return (
    <div className="notice notice-error" role="alert">
      <strong>{lead}</strong>
      {apiError && lead !== apiError.message ? <span>{apiError.message}</span> : null}
      {recovery ? <small>下一步：{recovery.next}</small> : null}
      {apiError?.requestId ? <small>请求编号：{apiError.requestId}</small> : null}
    </div>
  )
}

export function LoadingState({ label = "正在加载" }: { label?: string }) {
  return (
    <p className="loading" role="status">
      {label}…
    </p>
  )
}
