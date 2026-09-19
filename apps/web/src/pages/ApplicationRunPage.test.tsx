// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

/**
 * The ApplicationRunPage renders the run's status **twice**: once in the header's
 * metadata row (`RunStatusBadge`) and once on each timeline row, whose badge is the
 * status the event carried (PORT-005 made the timeline readable and added that
 * badge). A page-wide `getByText("已完成", { exact: true })` therefore resolves to
 * two elements and Playwright's strict mode rejects it, which is why the browser
 * specs scope those assertions to `.detail-meta`.
 *
 * These tests pin both halves of that: the header row names the run status, and the
 * status is rendered in more than one place. If the duplication ever goes away the
 * second test fails on purpose — the specs' scoping should then be relaxed rather
 * than left in place for a reason that no longer exists.
 */

let EVENTS: unknown[] = []

vi.mock("../api/client", () => ({
  api: {
    getApplicationRun: vi.fn(),
    retryApplicationRun: vi.fn(),
    cancelApplicationRun: vi.fn(),
  },
}))

vi.mock("../state/session", () => ({
  useAppStore: () => ({ accessToken: "tok" }),
}))

vi.mock("../api/sse", () => ({
  connectRunEvents: (
    _runId: string,
    opts: {
      onOpen?: () => void
      onEvent?: (ev: unknown) => void
      onClosed?: (reason: string) => void
    },
  ) => {
    opts.onOpen?.()
    for (const event of EVENTS) opts.onEvent?.(event)
    opts.onClosed?.("terminal")
    return { close: () => {} }
  },
}))

import { api } from "../api/client"
import { ApplicationRunPage } from "./ApplicationRunPage"

function event(sequence: number, status: string, messageKey: string, node: string | null) {
  return {
    event_id: `e${sequence}`,
    run_id: "r1",
    run_type: "APPLICATION",
    sequence,
    event_type: "STATUS_CHANGED",
    node,
    status,
    message_key: messageKey,
    safe_payload: {},
    occurred_at: "2026-09-19T00:00:00Z",
  }
}

const pendingScheduleApproval = {
  id: "ap1",
  application_run_id: "r1",
  action_type: "CREATE_INTERVIEW_SCHEDULE",
  status: "PENDING",
  original_params: { duration_minutes: 45, timezone: "UTC" },
  final_params: null,
  expected_application_version: 3,
  idempotency_key: "k1",
  version: 1,
  expires_at: null,
  decided_by: null,
  decided_at: null,
}

async function renderRun(run: Record<string, unknown>) {
  ;(api.getApplicationRun as ReturnType<typeof vi.fn>).mockResolvedValue(run)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/application-runs/r1"]}>
        <Routes>
          <Route path="/application-runs/:runId" element={<ApplicationRunPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await waitFor(() =>
    expect(screen.getByRole("heading", { name: "单人招聘流程" })).toBeInTheDocument(),
  )
}

describe("ApplicationRunPage run status", () => {
  it("names a completed run in the header metadata row", async () => {
    EVENTS = [event(1, "COMPLETED", "run.completed", null)]
    await renderRun({
      run_id: "r1",
      application_id: "a1",
      attempt: 1,
      status: "COMPLETED",
      completion_reason: "SUCCESS",
      current_approval: null,
      question_set: null,
      interview_id: null,
      interview_status: null,
      interview_external_id: null,
    })

    const header = document.querySelector(".detail-meta")
    expect(header).not.toBeNull()
    expect(header?.textContent).toContain("已完成")
  })

  it("names a run paused for approval in the header metadata row", async () => {
    EVENTS = [event(1, "WAITING_APPROVAL", "run.interrupted", "wait_schedule_approval")]
    await renderRun({
      run_id: "r1",
      application_id: "a1",
      attempt: 1,
      status: "WAITING_APPROVAL",
      completion_reason: null,
      current_approval: pendingScheduleApproval,
      question_set: null,
      interview_id: null,
      interview_status: null,
      interview_external_id: null,
    })

    const header = document.querySelector(".detail-meta")
    expect(header?.textContent).toContain("等待审批")
  })

  it("renders the status in more than one place, which is why the specs scope it", async () => {
    EVENTS = [event(1, "WAITING_APPROVAL", "run.interrupted", "wait_schedule_approval")]
    await renderRun({
      run_id: "r1",
      application_id: "a1",
      attempt: 1,
      status: "WAITING_APPROVAL",
      completion_reason: null,
      current_approval: pendingScheduleApproval,
      question_set: null,
      interview_id: null,
      interview_status: null,
      interview_external_id: null,
    })

    const exact = screen.queryAllByText("等待审批", { exact: true })
    expect(exact.length).toBeGreaterThanOrEqual(2)
    const inHeader = exact.filter((node) => node.closest(".detail-meta") !== null)
    expect(inHeader).toHaveLength(1)
  })
})
