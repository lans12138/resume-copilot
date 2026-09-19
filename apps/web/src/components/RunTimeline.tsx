import { useEffect, useRef, useState } from "react"
import { useNavigate } from "react-router-dom"
import { connectRunEvents, type SseError } from "../api/sse"
import type { AgentEventEnvelope, RunType } from "../api/types"
import { describeEvent, type TimelineRow } from "../lib/runTimeline"
import { useAppStore } from "../state/session"

type ConnState = "connecting" | "live" | "error" | "closed"

interface RunTimelineProps {
  runId: string
  runType: RunType
  /** Called once the stream closes on a terminal status (§13.5). */
  onTerminal?: () => void
}

/**
 * What each connection state means for the person watching, and what to do about
 * it (PORT-005). "已结束" alone leaves an operator unsure whether the run finished,
 * failed, or whether the browser simply stopped listening — the three cases need
 * different next steps.
 */
const CONN_TEXT: Record<ConnState, string> = {
  connecting: "连接中",
  live: "实时同步",
  error: "连接异常",
  closed: "已结束",
}

function TimelineItem({ event }: { event: AgentEventEnvelope }) {
  const row: TimelineRow = describeEvent(event)
  return (
    <li>
      <div className="tl-meta">
        <span>#{event.sequence}</span>
        <span>{row.title}</span>
        <span className={`badge badge-${row.tone}`}>{row.status}</span>
        {event.occurred_at ? (
          <span>{new Date(event.occurred_at).toLocaleTimeString("zh-CN")}</span>
        ) : null}
      </div>
      {row.detail ? <p className="tl-text">{row.detail}</p> : null}
      {row.failure ? (
        <p className="tl-failure">
          <strong>{row.failure.summary}</strong>
          {row.failure.code ? <code>{row.failure.code}</code> : null}
          {row.failure.candidate ? <small>候选人 {row.failure.candidate}</small> : null}
        </p>
      ) : null}
      {/* PORT-005: the identifiers stay available but out of the reading line —
          an operator recovers a failed run from the code, and a presenter should
          never have to say `node.score_with_evidence.failed` out loud. */}
      <details className="tl-detail">
        <summary>技术详情</summary>
        <dl className="kv">
          <dt>事件类型</dt>
          <dd><code>{row.technical.event_type}</code></dd>
          <dt>节点</dt>
          <dd><code>{row.technical.node ?? "—"}</code></dd>
          <dt>消息键</dt>
          <dd><code>{row.technical.message_key || "—"}</code></dd>
          {row.technical.payload ? (
            <>
              <dt>载荷</dt>
              <dd><code>{row.technical.payload}</code></dd>
            </>
          ) : null}
        </dl>
      </details>
    </li>
  )
}

/**
 * Live event timeline driven by the fetch-based SSE client. The client already
 * deduplicates by `sequence` and reconnects on gaps (§13.5); here we only render
 * and surface connection state. A 401 drops the session and returns to login.
 */
export function RunTimeline({ runId, runType, onTerminal }: RunTimelineProps) {
  const token = useAppStore((state) => state.accessToken)!
  const clearSession = useAppStore((state) => state.clearSession)
  const navigate = useNavigate()
  const [events, setEvents] = useState<AgentEventEnvelope[]>([])
  const [conn, setConn] = useState<ConnState>("connecting")
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const onTerminalRef = useRef(onTerminal)
  onTerminalRef.current = onTerminal

  useEffect(() => {
    setEvents([])
    setConn("connecting")
    setErrorMsg(null)
    const connection = connectRunEvents(runId, {
      runType,
      token,
      onOpen: () => setConn("live"),
      onEvent: (ev) =>
        setEvents((prev) => {
          if (prev.some((e) => e.sequence === ev.sequence)) return prev
          return [...prev, ev].sort((a, b) => a.sequence - b.sequence)
        }),
      onError: (err: SseError) => {
        setConn("error")
        setErrorMsg(err.message)
        if (err.kind === "unauthorized") {
          clearSession()
          navigate("/login", { replace: true })
        }
      },
      onClosed: (reason) => {
        if (reason === "terminal") {
          setConn("closed")
          onTerminalRef.current?.()
        } else if (reason === "client") {
          setConn("closed")
        }
      },
    })
    return () => connection.close()
  }, [runId, runType, token, clearSession, navigate])

  const connText = CONN_TEXT[conn]
  const connected = conn === "live"

  return (
    <section className="panel" aria-label="执行时间线">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Run timeline</p>
          <h2>执行时间线</h2>
        </div>
        <span className={`run-conn ${conn === "live" ? "live" : conn === "error" ? "error" : ""}`}>
          <span className="dot" aria-hidden="true" />
          {connText}
        </span>
      </div>
      {/* The reason must outlive the transition it causes. A revocation reports
          through `onError` and then closes the stream at once, so gating the
          notice on `conn === "error"` hid it behind "已结束" and the user was
          told the stream stopped without being told why — the one thing §13.4
          asks the browser to surface. `errorMsg` is only ever set by `onError`,
          and every path that sets it also closes the connection, so showing it
          whenever it exists cannot leave a stale notice behind. */}
      {errorMsg ? <div className="notice notice-error"><strong>{errorMsg}</strong></div> : null}
      {events.length === 0 ? (
        <p className="muted">
          等待事件流…（{connText}）
          {connected ? " 事件会随执行逐步出现，无需刷新页面。" : " 若长时间没有事件，请刷新页面重新订阅。"}
        </p>
      ) : (
        <>
          {connected ? null : (
            <p className="muted">
              当前{connText}；下方是已经收到的 {events.length} 条事件，执行结果仍可通过上方状态判断。
            </p>
          )}
          <ul className="timeline">
            {events.map((ev) => <TimelineItem key={ev.sequence} event={ev} />)}
          </ul>
        </>
      )}
    </section>
  )
}
