import { act, cleanup, render, screen, within } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { AgentEventEnvelope } from "../api/types"
import { useAppStore } from "../state/session"
import { RunTimeline } from "./RunTimeline"

interface CapturedHandlers {
  onEvent: (ev: AgentEventEnvelope) => void
  onOpen?: () => void
  onError?: (err: { kind: string; message: string }) => void
  onClosed?: (reason: string) => void
}

// `connectRunEvents` is replaced wholesale: this test is about what the timeline
// renders, and driving a real fetch-based stream would test the SSE client (which
// has its own suite) instead.
const sse = vi.hoisted(() => ({
  handlers: null as CapturedHandlers | null,
  close: () => {},
}))

vi.mock("../api/sse", async () => {
  const actual = await vi.importActual<typeof import("../api/sse")>("../api/sse")
  return {
    ...actual,
    connectRunEvents: (_runId: string, opts: CapturedHandlers) => {
      sse.handlers = opts
      return { close: sse.close }
    },
  }
})

function event(overrides: Partial<AgentEventEnvelope> = {}): AgentEventEnvelope {
  return {
    event_id: "e-1",
    run_id: "r-1",
    run_type: "APPLICATION",
    sequence: 1,
    event_type: "NODE_STARTED",
    node: null,
    status: "RUNNING",
    message_key: "",
    safe_payload: {},
    occurred_at: null,
    ...overrides,
  }
}

function renderTimeline() {
  return render(
    <MemoryRouter>
      <RunTimeline runId="r-1" runType="APPLICATION" />
    </MemoryRouter>,
  )
}

function deliver(events: AgentEventEnvelope[]) {
  act(() => {
    for (const ev of events) sse.handlers?.onEvent(ev)
  })
}

function open() {
  act(() => sse.handlers?.onOpen?.())
}

beforeEach(() => {
  sse.handlers = null
  useAppStore.setState({ accessToken: "test-token", currentUser: null })
})

afterEach(() => {
  cleanup()
  useAppStore.setState({ accessToken: null })
})

describe("RunTimeline", () => {
  // PORT-005: the row used to read `NODE_COMPLETED score_with_evidence
  // node.score_with_evidence.failed` — three vocabularies at once.
  it("names a node event in words instead of printing the node id", () => {
    renderTimeline()
    deliver([event({ event_type: "NODE_STARTED", node: "retrieve_candidates" })])

    const row = screen.getByRole("listitem")
    expect(within(row).getByText("召回候选人 · 开始")).toBeInTheDocument()
    // The id is still available — but only inside the collapsed detail panel, not in
    // the line the presenter reads.
    expect(row.querySelector(".tl-meta")?.textContent).not.toContain("retrieve_candidates")
    expect(
      within(row.querySelector("details.tl-detail")!).getByText("retrieve_candidates"),
    ).toBeInTheDocument()
  })

  it("keeps the identifiers one click away for whoever needs them", () => {
    renderTimeline()
    deliver([
      event({
        event_type: "NODE_COMPLETED",
        node: "score_with_evidence",
        message_key: "node.score_with_evidence.completed",
        safe_payload: { candidate_profile_id: "abc" },
      }),
    ])

    expect(screen.getByText("技术详情")).toBeInTheDocument()
    expect(screen.getByText("score_with_evidence")).toBeInTheDocument()
    expect(screen.getByText("node.score_with_evidence.completed")).toBeInTheDocument()
  })

  it("says why a run failed and which code to quote", () => {
    renderTimeline()
    deliver([
      event({
        event_type: "RUN_FAILED",
        node: "human_review",
        status: "FAILED",
        message_key: "run.failed",
        safe_payload: { reason: "APPROVAL_EXPIRED", error_code: "APPROVAL_EXPIRED", retryable: true },
      }),
    ])

    expect(screen.getByText("流程失败")).toBeInTheDocument()
    expect(screen.getByText("审批超时未决策，流程已失败")).toBeInTheDocument()
    expect(screen.getByText("APPROVAL_EXPIRED")).toBeInTheDocument()
  })

  it("distinguishes waiting, completed and failed at a glance", () => {
    renderTimeline()
    deliver([
      event({ sequence: 1, event_type: "STATUS_CHANGED", node: "human_review", status: "WAITING_APPROVAL", message_key: "run.interrupted" }),
      event({ sequence: 2, event_type: "RUN_COMPLETED", status: "COMPLETED", message_key: "run.completed" }),
      event({ sequence: 3, event_type: "RUN_FAILED", status: "FAILED", message_key: "run.failed", safe_payload: { error_code: "ACTION_REJECTED" } }),
    ])

    expect(screen.getByText("等待审批")).toBeInTheDocument()
    expect(screen.getByText("已完成")).toBeInTheDocument()
    expect(screen.getByText("失败")).toBeInTheDocument()
    expect(screen.getByText("人工审批驳回")).toBeInTheDocument()
  })

  it("says a partial list is partial when the stream dropped", () => {
    renderTimeline()
    deliver([event({ event_type: "NODE_STARTED", node: "finalize_run" })])
    act(() => sse.handlers?.onClosed?.("network"))

    expect(screen.getByText(/已经收到的 1 条事件/)).toBeInTheDocument()
  })

  it("keeps telling the user why the stream stopped", () => {
    // §13.4: a revocation reports through `onError` and closes at once; the reason
    // must outlive the transition that causes it.
    renderTimeline()
    act(() => {
      sse.handlers?.onError?.({ kind: "revoked", message: "登录已失效，事件流已断开" })
      sse.handlers?.onClosed?.("network")
    })

    expect(screen.getByText("登录已失效，事件流已断开")).toBeInTheDocument()
  })

  it("tells the reader what to do while waiting for the first event", () => {
    renderTimeline()

    expect(screen.getByText(/等待事件流/)).toBeInTheDocument()
    expect(screen.getByText(/若长时间没有事件，请刷新页面重新订阅/)).toBeInTheDocument()
  })

  it("stops asking for a refresh once the stream is live", () => {
    renderTimeline()
    open()
    deliver([event({ event_type: "NODE_STARTED", node: "load_application" })])

    const rows = screen.getAllByRole("listitem")
    expect(within(rows[0]).getByText("载入申请 · 开始")).toBeInTheDocument()
    expect(screen.queryByText(/请刷新页面重新订阅/)).toBeNull()
  })
})
