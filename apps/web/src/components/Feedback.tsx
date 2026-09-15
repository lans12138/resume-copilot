import { ApiError } from "../api/client"

const statusLead: Record<number, string> = {
  403: "当前账号没有执行此操作的权限。",
  409: "页面数据已发生变化，请刷新后重试。",
  422: "提交内容未通过校验，请检查各字段。",
  503: "核心依赖暂时不可用，请稍后重试。",
}

export function ErrorNotice({ error }: { error: unknown }) {
  const apiError = error instanceof ApiError ? error : null
  const lead = apiError ? (statusLead[apiError.status] ?? apiError.message) : "出现未知错误，请重试。"
  return <div className="notice notice-error" role="alert">
    <strong>{lead}</strong>
    {apiError && lead !== apiError.message ? <span>{apiError.message}</span> : null}
    {apiError?.requestId ? <small>请求编号：{apiError.requestId}</small> : null}
  </div>
}

export function LoadingState({ label = "正在加载" }: { label?: string }) {
  return <p className="loading" role="status">{label}…</p>
}
