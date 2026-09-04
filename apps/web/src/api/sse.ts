import type { AgentEventEnvelope, RunType } from "./types"

/** Terminal run statuses: once seen, the stream closes and the Run Query is invalidated (§13.5). */
export const TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  "COMPLETED",
  "FAILED",
  "CANCELLED",
])
export const SSE_AUTH_REVOKED = "SSE_AUTH_REVOKED"

export type SseBlockKind = "event" | "heartbeat" | "auth_revoked"

export interface SseBlock {
  kind: SseBlockKind
  event?: AgentEventEnvelope
}

/**
 * Parse a `text/event-stream` text buffer into blocks, returning the incomplete
 * trailing segment for the next call. Pure and side-effect free so it can be
 * unit tested (§19.6: 去重、跳号、终态、心跳解析).
 */
export function parseSseBlocks(buffer: string): { blocks: SseBlock[]; rest: string } {
  const segments = buffer.split("\n\n")
  const rest = segments.pop() ?? ""
  const blocks: SseBlock[] = []
  for (const raw of segments) {
    const lines = raw.split("\n")
    let eventType: string | null = null
    let data = ""
    let isComment = false
    for (const line of lines) {
      if (line.startsWith(":")) {
        isComment = true
        continue
      }
      if (line.startsWith("event:")) eventType = line.slice(6).trim()
      else if (line.startsWith("data:")) data += (data ? "\n" : "") + line.slice(5).trim()
      // "id:" and "retry:" carry metadata the client tracks separately.
    }
    if (eventType === SSE_AUTH_REVOKED) {
      blocks.push({ kind: "auth_revoked" })
      continue
    }
    if (isComment && !eventType && !data) {
      blocks.push({ kind: "heartbeat" })
      continue
    }
    if (eventType && data) {
      try {
        blocks.push({ kind: "event", event: JSON.parse(data) as AgentEventEnvelope })
      } catch {
        // Malformed data frame: ignore rather than kill the stream.
      }
    }
  }
  return { blocks, rest }
}

/**
 * Decide how to handle an incoming sequence against the last accepted one
 * (§13.5: `sequence <= lastAccepted` 丢弃；跳号则断开重连).
 */
export function classifySequence(
  sequence: number,
  lastAccepted: number,
): "accept" | "duplicate" | "gap" {
  if (sequence <= lastAccepted) return "duplicate"
  if (sequence > lastAccepted + 1) return "gap"
  return "accept"
}

export type SseErrorKind = "unauthorized" | "forbidden" | "revoked" | "gap"
export interface SseError {
  kind: SseErrorKind
  message: string
}

export interface SseHandlers {
  onEvent: (ev: AgentEventEnvelope) => void
  onOpen?: () => void
  onError?: (err: SseError) => void
  onClosed?: (reason: "terminal" | "client" | "network") => void
}

export interface SseConnection {
  close: () => void
}

export interface ConnectOptions {
  runType: RunType
  token: string
  basePath?: string
  onEvent: (ev: AgentEventEnvelope) => void
  onOpen?: () => void
  onError?: (err: SseError) => void
  onClosed?: (reason: "terminal" | "client" | "network") => void
  /** Override for tests; defaults to the browser global. */
  fetchImpl?: typeof fetch
  maxBackoffMs?: number
  signal?: AbortSignal
}

/**
 * Browser SSE client built on `fetch` (not `EventSource`) so the `Authorization`
 * header can be set — `EventSource` cannot send custom headers (§13.5). The
 * client maintains `lastAccepted`, deduplicates by sequence, reconnects on a gap
 * or network error with jittered exponential backoff (max 30s), stops on
 * 401/403/revocation, and closes cleanly on a terminal status.
 */
export function connectRunEvents(
  runId: string,
  opts: ConnectOptions,
): SseConnection {
  const basePath = opts.basePath ?? "/api/v1"
  const doFetch = opts.fetchImpl ?? fetch
  const maxBackoff = opts.maxBackoffMs ?? 30_000
  const controller = new AbortController()
  let lastAccepted = -1
  let closed = false
  let attempt = 0
  let buffer = ""

  function stop(reason: "terminal" | "client" | "network") {
    if (closed) return
    closed = true
    controller.abort()
    opts.onClosed?.(reason)
  }

  function pathFor(): string {
    const prefix = opts.runType === "MATCH" ? "match-runs" : "application-runs"
    return `${basePath}/${prefix}/${runId}/events`
  }

  async function open() {
    if (closed) return
    const headers: Record<string, string> = {
      Accept: "text/event-stream",
      Authorization: `Bearer ${opts.token}`,
    }
    if (lastAccepted >= 0) headers["Last-Event-ID"] = String(lastAccepted)
    let response: Response
    try {
      response = await doFetch(pathFor(), { headers, signal: controller.signal })
    } catch {
      if (closed) return
      scheduleReconnect()
      return
    }
    if (response.status === 401) {
      opts.onError?.({ kind: "unauthorized", message: "身份失效，请重新登录" })
      stop("client")
      return
    }
    if (response.status === 403) {
      opts.onError?.({ kind: "forbidden", message: "权限已变更" })
      stop("client")
      return
    }
    if (!response.ok || !response.body) {
      scheduleReconnect()
      return
    }
    opts.onOpen?.()
    attempt = 0
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    try {
      while (!closed) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parsed = parseSseBlocks(buffer)
        buffer = parsed.rest
        for (const block of parsed.blocks) {
          if (block.kind === "heartbeat") continue
          if (block.kind === "auth_revoked") {
            opts.onError?.({ kind: "revoked", message: "授权已撤销" })
            stop("client")
            return
          }
          const ev = block.event!
          const verdict = classifySequence(ev.sequence, lastAccepted)
          if (verdict === "duplicate") continue
          if (verdict === "gap") {
            opts.onError?.({ kind: "gap", message: "事件跳号，重新连接" })
            stop("client")
            reconnectFrom(lastAccepted)
            return
          }
          lastAccepted = ev.sequence
          opts.onEvent(ev)
          if (TERMINAL_STATUSES.has(ev.status)) {
            stop("terminal")
            return
          }
        }
      }
    } catch {
      if (closed) return
      scheduleReconnect()
    }
  }

  function scheduleReconnect() {
    if (closed) return
    attempt += 1
    const base = Math.min(1000 * 2 ** (attempt - 1), maxBackoff)
    const delay = Math.min(maxBackoff, base + Math.random() * 300)
    setTimeout(() => {
      if (!closed) void open()
    }, delay)
  }

  function reconnectFrom(seq: number) {
    lastAccepted = seq
    attempt = 0
    void open()
  }

  opts.signal?.addEventListener("abort", () => stop("client"))
  void open()
  return { close: () => stop("client") }
}
