import { useQuery } from "@tanstack/react-query"
import { api } from "../api/client"
import { useAppStore } from "../state/session"

/**
 * Which model produced what is on screen (PORT-005).
 *
 * A recorded evaluation, a match explanation and a report all prove different things
 * depending on the adapter behind them, and nothing in the UI said which one it was —
 * a viewer could not tell a real model's answer from a scripted one. The banner states
 * it once, on every page, because the answer applies to the whole deployment rather
 * than to one screen.
 *
 * A failure to read the mode is shown rather than hidden: "we could not confirm the
 * model mode" is a different statement from "it is not mock", and silently rendering
 * nothing would let a viewer assume the safer of the two.
 */
export function ModelModeBanner() {
  const token = useAppStore((state) => state.accessToken)
  const modeQuery = useQuery({
    queryKey: ["runtime", "model-mode"],
    queryFn: () => api.getModelMode(token!),
    enabled: Boolean(token),
    // The mode is fixed for the life of the process; re-reading it on every navigation
    // would be a request per page for a constant.
    staleTime: Infinity,
  })

  if (modeQuery.isLoading || (modeQuery.error === null && modeQuery.data === undefined)) return null

  if (modeQuery.error !== null || modeQuery.data === undefined) {
    return (
      <div className="model-mode is-unknown" role="status">
        <span className="badge badge-warn">模型模式未知</span>
        <span>无法确认本次运行使用的是真实模型还是 Mock 模型，请勿据本页结论推断模型效果。</span>
      </div>
    )
  }

  const mode = modeQuery.data
  return (
    <div className={`model-mode ${mode.mock_model_mode ? "is-mock" : "is-live"}`} role="status">
      <span className={`badge badge-${mode.mock_model_mode ? "warn" : "ok"}`}>{mode.source_label}</span>
      <span>
        {mode.mock_model_mode
          ? "匹配解释与向量由确定性假模型生成，不调用外部服务：可以证明流程、契约与可复现性，不能证明真实模型效果。"
          : "匹配解释与向量由配置的外部模型生成。"}
      </span>
      <details className="model-mode-detail">
        <summary>模型详情</summary>
        <dl className="kv">
          <dt>对话模型</dt>
          <dd><code>{mode.chat_model}</code></dd>
          <dt>向量模型</dt>
          <dd><code>{mode.embedding_model}</code>（{mode.embedding_dimension} 维）</dd>
          <dt>提示词版本</dt>
          <dd><code>{mode.prompt_version}</code></dd>
          <dt>规则版本</dt>
          <dd><code>{mode.rule_version}</code></dd>
        </dl>
      </details>
    </div>
  )
}
