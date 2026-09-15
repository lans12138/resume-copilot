import { useEffect, useRef, useState } from "react"
import { useNavigate } from "react-router-dom"
import { connectRunEvents, type SseError } from "../api/sse"
import type { AgentEventEnvelope, RunType } from "../api/types"
import { useAppStore } from "../state/session"

const eventLabel: Record<string, string> = {
  RUN_CREATED: "流程创建",
  NODE_STARTED: "节点开始",
  NODE_COMPLETED: "节点完成",
  RUN_RESUMED: "流程恢复",
  STATUS_CHANGED: "状态变更",
  RUN_COMPLETED: "流程完成",
  RUN_FAILED: "流程失败",
  RUN_CANCELLED: "流程取消",
}

type ConnState = "connecting" | "live" | "error" | "closed"

interface RunTimelineProps {
  runId: string
  runType: RunType
  /** Called once the stream closes on a terminal status (§13.5). */
  onTerminal?: () => void
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

  const connText =
    conn === "connecting" ? "连接中" : conn === "live" ? "实时同步" : conn === "error" ? "连接异常" : "已结束"

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
      {conn === "error" && errorMsg ? <div className="notice notice-error"><strong>{errorMsg}</strong></div> : null}
      {events.length === 0 ? (
        <p className="muted">等待事件流…（{connText}）</p>
      ) : (
        <ul className="timeline">
          {events.map((ev) => (
            <li key={ev.sequence}>
              <div className="tl-meta">
                <span>#{ev.sequence}</span>
                <span>{eventLabel[ev.event_type] ?? ev.event_type}</span>
                {ev.node ? <code>{ev.node}</code> : null}
                {ev.occurred_at ? <span>{new Date(ev.occurred_at).toLocaleTimeString("zh-CN")}</span> : null}
              </div>
              {ev.message_key && ev.message_key !== ev.event_type ? (
                <p className="tl-text">{ev.message_key}</p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
