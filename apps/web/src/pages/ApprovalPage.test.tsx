import { cleanup, render, screen, waitFor, within } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { ApprovalDetail, ApprovalStatus } from "../api/types"
import { useAppStore } from "../state/session"
import { ApprovalPage } from "./ApprovalPage"

const APPROVAL_ID = "11111111-1111-4111-8111-111111111111"
const RUN_ID = "22222222-2222-4222-8222-222222222222"

function approval(status: ApprovalStatus, overrides: Partial<ApprovalDetail> = {}): ApprovalDetail {
  return {
    id: APPROVAL_ID,
    application_run_id: RUN_ID,
    action_type: "UPDATE_APPLICATION_STATUS",
    status,
    original_params: { target_status: "SHORTLISTED" },
    final_params: null,
    expected_application_version: 7,
    idempotency_key: "run-1:UPDATE_APPLICATION_STATUS",
    version: 1,
    expires_at: null,
    decided_by: null,
    decided_at: null,
    ...overrides,
  }
}

/** Serve a sequence of GET responses, then repeat the last one. */
function stubApproval(sequence: ApprovalDetail[]) {
  let index = 0
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      const body = sequence[Math.min(index, sequence.length - 1)]
      index += 1
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    }),
  )
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/approvals/${APPROVAL_ID}`]}>
        <Routes>
          <Route path="/approvals/:approvalId" element={<ApprovalPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** The page repeats the status in the header and in the result panel on purpose, so
 * assertions scope to the panel that owns the claim. */
function panel(name: string): HTMLElement {
  return screen.getByRole("region", { name })
}

beforeEach(() => {
  useAppStore.setState({ accessToken: "test-token", currentUser: null })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  useAppStore.setState({ accessToken: null })
})

describe("ApprovalPage", () => {
  // PORT-005: the page used to show a status badge and a raw payload dump. A person
  // authorising a write has to be able to say what it will do.
  it("states the action in a sentence, not just as a parameter dump", async () => {
    stubApproval([approval("PENDING")])
    renderPage()

    expect(await screen.findByRole("heading", { name: "拟执行动作" })).toBeInTheDocument()
    expect(
      within(panel("拟执行动作")).getByText("将申请状态更新为「已入围」。"),
    ).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "更新申请状态" })).toBeInTheDocument()
  })

  it("shows the version the decision is taken against", async () => {
    stubApproval([approval("PENDING")])
    renderPage()

    const versions = within(await screen.findByRole("region", { name: "当前版本" }))
    expect(versions.getByText("决策依据的申请版本")).toBeInTheDocument()
    expect(versions.getByText("v7")).toBeInTheDocument()
    expect(versions.getByText("run-1:UPDATE_APPLICATION_STATUS")).toBeInTheDocument()
    expect(versions.getByText(/执行时还会比对申请版本/)).toBeInTheDocument()
  })

  it("does not claim a decision has been applied while the executor has not run", async () => {
    // APPROVED means the decision is recorded; the side effect is a separate step.
    stubApproval([approval("APPROVED")])
    renderPage()

    await screen.findByRole("heading", { name: "执行结果" })
    const result = within(panel("执行结果"))
    expect(result.getByText("已通过，等待执行")).toBeInTheDocument()
    expect(result.getByText(/尚未写入/)).toBeInTheDocument()
    expect(result.getByText(/本页会自动刷新执行结果/)).toBeInTheDocument()
  })

  it("converges onto the applied result once the executor has run", async () => {
    stubApproval([approval("APPROVED"), approval("EXECUTED")])
    renderPage()

    await screen.findByRole("heading", { name: "执行结果" })
    expect(within(panel("执行结果")).getByText("已通过，等待执行")).toBeInTheDocument()
    // The poll interval is 1s; the assertion is about the page reaching the final
    // fact on its own, not about the interval's exact value.
    await waitFor(
      () => expect(within(panel("执行结果")).getByText("已执行")).toBeInTheDocument(),
      { timeout: 4000 },
    )
    expect(within(panel("执行结果")).getByText(/只应用了一次/)).toBeInTheDocument()
  })

  it("offers the retry path when execution failed", async () => {
    stubApproval([approval("EXECUTION_FAILED")])
    renderPage()

    await screen.findByRole("heading", { name: "执行结果" })
    const result = within(panel("执行结果"))
    expect(result.getByText("执行失败")).toBeInTheDocument()
    expect(result.getByText(/不存在部分写入/)).toBeInTheDocument()
    expect(result.getByText("下一步")).toBeInTheDocument()
    expect(result.getByRole("link", { name: "前往申请流程重试 →" })).toBeInTheDocument()
  })

  it("says a rejected approval wrote nothing and cannot be decided again", async () => {
    stubApproval([approval("REJECTED")])
    renderPage()

    expect(await screen.findByText("该审批已不可决策")).toBeInTheDocument()
    expect(within(panel("执行结果")).getByText(/没有执行任何写入/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "通过" })).toBeNull()
  })

  it("marks an edited parameter as changed, with both sides", async () => {
    stubApproval([
      approval("PENDING", {
        original_params: { target_status: "SHORTLISTED" },
        final_params: { target_status: "ON_HOLD" },
      }),
    ])
    renderPage()

    const diff = within(await screen.findByRole("region", { name: "动作差异" }))
    expect(diff.getByText("已修改")).toBeInTheDocument()
    expect(diff.getByText("已入围（SHORTLISTED）")).toBeInTheDocument()
    expect(diff.getByText("暂缓（ON_HOLD）")).toBeInTheDocument()
    // The action sentence follows the final parameters, not the original proposal.
    expect(within(panel("拟执行动作")).getByText("将申请状态更新为「暂缓」。")).toBeInTheDocument()
  })
})
