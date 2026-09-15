import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter } from "react-router-dom"
import { EvaluationsPage } from "./EvaluationsPage"
import { useAppStore } from "../state/session"
import type { EvaluationDetail, EvaluationRunSummary, MetricSnapshotView } from "../api/types"

const RUN_ID = "11111111-1111-4111-8111-111111111111"
const DATASET_ID = "22222222-2222-4222-8222-222222222222"

const failingRun: EvaluationRunSummary = {
  id: RUN_ID,
  dataset_version_id: DATASET_ID,
  kind: "SEMANTIC",
  status: "COMPLETED",
  config_hash: "a".repeat(64),
  passed: false,
  error_code: null,
  created_at: "2026-09-15T02:00:00Z",
  started_at: "2026-09-15T02:00:01Z",
  finished_at: "2026-09-15T02:00:09Z",
  failing_metric_count: 1,
}

const passingMetrics: MetricSnapshotView[] = [
  { metric_name: "recall_at_k", metric_value: 0.966667, threshold: 0.85, passed: true, dimensions: {} },
  { metric_name: "support_macro_f1", metric_value: 0.91, threshold: 0.85, passed: true, dimensions: {} },
  { metric_name: "num_cases", metric_value: 3, threshold: null, passed: true, dimensions: {} },
]

const failingMetrics: MetricSnapshotView[] = [
  { metric_name: "supported_precision", metric_value: 0.9, threshold: 0.95, passed: false, dimensions: {} },
  { metric_name: "support_macro_f1", metric_value: 0.91, threshold: 0.85, passed: true, dimensions: {} },
  { metric_name: "injection_control_flow_changes", metric_value: 2, threshold: 0, passed: false, dimensions: {} },
]

function detailBody(metrics: MetricSnapshotView[], run: EvaluationRunSummary = failingRun): EvaluationDetail {
  return {
    run,
    dataset: {
      id: DATASET_ID,
      name: "support-labels",
      version: "v1",
      content_hash: "b".repeat(64),
      schema_version: "1",
      manifest: { claims: 5 },
    },
    model_snapshot: { chat_model: "fake" },
    prompt_versions: { prompt_version: "v1" },
    error_message_safe: null,
    metrics,
  }
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <EvaluationsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  useAppStore.setState({ accessToken: "test-token", currentUser: null })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  useAppStore.setState({ accessToken: null })
})

describe("EvaluationsPage", () => {
  it("lists runs and renders the verdict from the server", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      items: [failingRun], total: 1, limit: 50, offset: 0,
    })))

    renderPage()

    expect(await screen.findByText("语义支持")).toBeInTheDocument()
    expect(screen.getByText("未达标")).toBeInTheDocument()
    // The failing count is the whole point of the list row.
    expect(screen.getByText("1")).toBeInTheDocument()
  })

  it("distinguishes a run with no verdict from a failing one", async () => {
    // A queued run has `passed: null`; rendering that as "未达标" would make every
    // in-flight evaluation look like a regression.
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      items: [{ ...failingRun, status: "RUNNING", passed: null, failing_metric_count: 0 }],
      total: 1, limit: 50, offset: 0,
    })))

    renderPage()

    expect(await screen.findByText("尚无结论")).toBeInTheDocument()
    expect(screen.queryByText("未达标")).not.toBeInTheDocument()
  })

  it("shows every thresholded metric with its bar when the run passes", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes(`/evaluations/${RUN_ID}`)) {
        return jsonResponse(detailBody(passingMetrics, { ...failingRun, passed: true, failing_metric_count: 0 }))
      }
      return jsonResponse({ items: [failingRun], total: 1, limit: 50, offset: 0 })
    }))

    renderPage()
    await userEvent.click(await screen.findByRole("button", { name: "查看指标" }))

    // The dataset identity is shown so a verdict can be attributed.
    expect(await screen.findByText((_, node) => node?.textContent === "数据集 support-labels@v1")).toBeInTheDocument()
    expect(screen.getByText("召回率 Recall@K")).toBeInTheDocument()
    expect(screen.getByText("0.9667")).toBeInTheDocument()
    // A threshold-less counter is informational only: it is shown but never
    // dressed up as a verdict, so it must not carry a 通过/未达标 chip.
    const infoRow = screen.getByText("num_cases").closest("tr")
    expect(infoRow).not.toBeNull()
    expect(within(infoRow as HTMLElement).getByText("参考")).toBeInTheDocument()
    expect(within(infoRow as HTMLElement).queryByText("通过")).not.toBeInTheDocument()
  })

  it("lists failing metrics first and states the count", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes(`/evaluations/${RUN_ID}`)) return jsonResponse(detailBody(failingMetrics))
      return jsonResponse({ items: [failingRun], total: 1, limit: 50, offset: 0 })
    }))

    renderPage()
    await userEvent.click(await screen.findByRole("button", { name: "查看指标" }))

    // The reader arrives asking "what broke", so failures must lead.
    const rows = await screen.findAllByRole("row")
    const dataRows = rows.filter((row) => within(row).queryByText("未达标"))
    expect(dataRows.length).toBeGreaterThan(0)
    const firstDataRow = rows[rows.indexOf(dataRows[0])]
    expect(within(firstDataRow).getByText("未达标")).toBeInTheDocument()

    // The summary states how many bars were missed.
    expect(screen.getByText(/有 2 项未达标/)).toBeInTheDocument()
  })

  it("marks an unthresholded metric as informational rather than passed", async () => {
    // An informational metric did not participate in the gate; rendering it as a
    // pass would credit the gate with a check it never performed.
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes(`/evaluations/${RUN_ID}`)) {
        return jsonResponse(detailBody(passingMetrics, { ...failingRun, passed: true, failing_metric_count: 0 }))
      }
      return jsonResponse({ items: [failingRun], total: 1, limit: 50, offset: 0 })
    }))

    renderPage()
    await userEvent.click(await screen.findByRole("button", { name: "查看指标" }))

    const row = (await screen.findByText("num_cases")).closest("tr")!
    expect(within(row).getByText("参考")).toBeInTheDocument()
    expect(within(row).queryByText("通过")).not.toBeInTheDocument()
  })

  it("renders a failed execution with its safe error code", async () => {
    const failedRun: EvaluationRunSummary = {
      ...failingRun,
      status: "FAILED",
      passed: null,
      error_code: "EVALUATION_EXECUTION_FAILED",
      failing_metric_count: 0,
    }
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes(`/evaluations/${RUN_ID}`)) {
        return jsonResponse({ ...detailBody([], failedRun), error_message_safe: "评测执行失败，请查看服务日志" })
      }
      return jsonResponse({ items: [failedRun], total: 1, limit: 50, offset: 0 })
    }))

    renderPage()
    await userEvent.click(await screen.findByRole("button", { name: "查看指标" }))

    expect(await screen.findByText("评测执行失败")).toBeInTheDocument()
    expect(screen.getByText("错误码：EVALUATION_EXECUTION_FAILED")).toBeInTheDocument()
    // A harness failure must not be presented as a threshold miss.
    expect(screen.queryByText("未达标")).not.toBeInTheDocument()
  })

  it("renders counts as integers rather than rates", async () => {
    // "2.000000" for an attack-success counter reads like a rate; "2" is a tally.
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes(`/evaluations/${RUN_ID}`)) return jsonResponse(detailBody(failingMetrics))
      return jsonResponse({ items: [failingRun], total: 1, limit: 50, offset: 0 })
    }))

    renderPage()
    await userEvent.click(await screen.findByRole("button", { name: "查看指标" }))

    const row = (await screen.findByText("注入改变控制流")).closest("tr")!
    expect(within(row).getByText("2")).toBeInTheDocument()
    expect(within(row).queryByText("2.0000")).not.toBeInTheDocument()
  })

  it("shows an empty state before any evaluation has run", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ items: [], total: 0, limit: 50, offset: 0 })))

    renderPage()

    expect(await screen.findByText(/还没有评测记录/)).toBeInTheDocument()
  })

  it("surfaces a load failure instead of an empty table", async () => {
    // An empty list and a failed request look identical to a reader unless the
    // error is rendered; that would hide an outage behind a plausible page.
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(
      { code: "FORBIDDEN", message: "当前用户无权执行此操作" }, 403,
    )))

    renderPage()

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument())
  })
})
