import { cleanup, render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
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

  // The browser specs navigate here and then ask the page which action is being
  // authorised. PORT-005 (`e3c072f`) moved that answer from the 动作差异 panel to the
  // page heading, which left them asserting a heading named "审批决策" — a name that
  // does exist in the app, as the decision button group's `aria-label`, and therefore
  // survives every text-level search — and an action name inside a panel that now
  // holds only the parameter diff. Pin the shape they rely on, so the next move is
  // caught here instead of in the browser gate.
  it("names the action in the page heading, not in the parameter diff", async () => {
    stubApproval([approval("PENDING")])
    renderPage()

    // Exactly one level-1 heading, and it is the action: the specs locate it by name
    // *and* level, so a second one would fail them in strict mode.
    const headings = await screen.findAllByRole("heading", { level: 1 })
    expect(headings).toHaveLength(1)
    expect(headings[0]).toHaveTextContent("更新申请状态")

    // "审批决策" labels the decision button group. It is not a heading, and a spec
    // that looks for it as one finds nothing.
    expect(screen.queryByRole("heading", { name: "审批决策" })).not.toBeInTheDocument()
    expect(screen.getByRole("group", { name: "审批决策" })).toBeInTheDocument()

    // The panel a shared "we are on the approval screen" helper can wait on, since
    // the heading it used to wait on is now action-specific.
    expect(panel("拟执行动作")).toBeInTheDocument()

    // And the panel that used to carry the action name no longer does.
    expect(panel("动作差异")).not.toHaveTextContent("更新申请状态")
  })

  // The two specs that approve an untouched approval asserted on the proposal's own
  // status token. It used to be printed raw; it is now the rendered parameter, which
  // is a different string, and the *count* of that string depends on this panel
  // falling back to the original — so both facts are pinned together.
  it("renders an untouched approval's parameters as unchanged, on both sides", async () => {
    stubApproval([approval("PENDING")])
    renderPage()

    const diff = within(await screen.findByRole("region", { name: "动作差异" }))
    // `final_params` is null until someone edits; that is "nothing changed yet", not
    // "everything was deleted". A `已删除` badge here would tell the reviewer the
    // opposite of the truth about what they are about to authorise.
    expect(diff.queryByText("已删除")).not.toBeInTheDocument()
    expect(diff.getByText("未改动")).toBeInTheDocument()
    // Both columns carry it, which is why a spec cannot address it with a bare
    // `getByText` — Playwright's strict mode rejects the second match.
    expect(diff.getAllByText("已入围（SHORTLISTED）")).toHaveLength(2)
    expect(diff.queryByText("—")).not.toBeInTheDocument()
  })

  it("names the status a submitted decision reached, in words", async () => {
    // The POST answers with the approval it just decided, so the notice reports that
    // status — `EXECUTED` for the first gate, because the side effect is applied
    // inside the request (`ApplicationRunService.decide_approval`).
    stubApproval([approval("PENDING"), approval("EXECUTED")])
    renderPage()

    await userEvent.click(await screen.findByRole("button", { name: "通过" }))

    // The label, not the token: `approval.ts` translates every status for the reader,
    // and the browser specs asserted the raw token until the translation landed.
    expect(await screen.findByText("决策已提交，当前状态：已执行")).toBeInTheDocument()
    expect(screen.queryByText(/决策已提交，当前状态：EXECUTED/)).not.toBeInTheDocument()
  })

  it("says a rejected decision wrote nothing, in words", async () => {
    stubApproval([approval("PENDING"), approval("REJECTED")])
    renderPage()

    await userEvent.click(await screen.findByRole("button", { name: "驳回" }))

    expect(await screen.findByText("决策已提交，当前状态：已驳回")).toBeInTheDocument()
  })
})
