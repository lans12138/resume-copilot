// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

// The SSE client performs real fetches; isolate this test from the network so it
// only exercises the hook ordering and heading render of MatchRunPage.
vi.mock("../components/RunTimeline", () => ({
  RunTimeline: () => <div data-testid="run-timeline" />,
}))

vi.mock("../api/client", () => ({
  api: {
    getMatchRun: vi.fn(),
    getReports: vi.fn(),
  },
}))

vi.mock("../state/session", () => ({
  useAppStore: () => ({ accessToken: "tok" }),
}))

import { api } from "../api/client"
import { MatchRunPage } from "./MatchRunPage"

const makeRun = (status: string) => ({
  run_id: "r1",
  job_id: "j1",
  job_version_id: "v1",
  rule_version: "1",
  created_at: new Date().toISOString(),
  status,
  candidates: [],
})

describe("MatchRunPage hook ordering", () => {
  it("renders the heading once the run query settles (no Rules-of-Hooks crash)", async () => {
    ;(api.getMatchRun as ReturnType<typeof vi.fn>).mockResolvedValue(makeRun("COMPLETED"))
    ;(api.getReports as ReturnType<typeof vi.fn>).mockResolvedValue({ reports: [] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/match-runs/r1"]}>
          <Routes>
            <Route path="/match-runs/:runId" element={<MatchRunPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    // The heading must appear after the run query resolves. A Rules-of-Hooks
    // violation (a hook declared after an early return) crashes the component on
    // the loading -> loaded transition, so the heading would never render.
    await waitFor(
      () => expect(screen.getByRole("heading", { name: "批量匹配分析" })).toBeInTheDocument(),
      { timeout: 3000 },
    )
  })
})
