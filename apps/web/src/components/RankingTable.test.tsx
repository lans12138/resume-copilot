import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router-dom"
import { describe, expect, it, vi } from "vitest"
import type { MatchRunCandidate } from "../api/types"
import { RankingTable } from "./RankingTable"

const completed: MatchRunCandidate = {
  candidate_profile_id: "11111111-1111-4111-8111-111111111111",
  application_id: "22222222-2222-4222-8222-222222222222",
  snapshot_order: 1,
  rrf_score: 0.125,
  processing_status: "COMPLETED",
  hard_rule_overall: "PASS",
  display_name: "张三",
  normalized_skills: ["python", "go", "sql", "docker", "k8s"],
  years_experience: 8,
  education_level: "MASTER",
  document_id: "33333333-3333-4333-8333-333333333333",
}

function renderTable(props: Partial<Parameters<typeof RankingTable>[0]> = {}) {
  return render(
    <MemoryRouter>
      <RankingTable candidates={[completed]} {...props} />
    </MemoryRouter>,
  )
}

describe("RankingTable", () => {
  it("starts an ApplicationRun with the durable application id", async () => {
    const onStartApplication = vi.fn()
    renderTable({ onStartApplication })

    await userEvent.click(screen.getByRole("button", { name: "启动单人流程" }))

    expect(onStartApplication).toHaveBeenCalledOnce()
    expect(onStartApplication).toHaveBeenCalledWith(completed.application_id)
  })

  it("does not start a candidate whose MatchRun processing failed", () => {
    renderTable({
      candidates: [{ ...completed, processing_status: "FAILED" }],
      onStartApplication: vi.fn(),
    })

    const row = screen.getByText("失败").closest("tr")!
    expect(within(row).getByRole("button", { name: "启动单人流程" })).toBeDisabled()
  })

  it("shows which application is being started", () => {
    renderTable({ onStartApplication: vi.fn(), startingApplicationId: completed.application_id })

    expect(screen.getByRole("button", { name: "启动中…" })).toBeDisabled()
  })

  // PORT-005: the presenter identifies a row by name, so the name is the primary
  // cell and the summary travels with it.
  it("leads the candidate cell with the name and a summary", () => {
    renderTable()

    const row = screen.getByText("张三").closest("tr")!
    expect(within(row).getByText("8 年经验 · 硕士 · python、go、sql、docker 等 5 项")).toBeInTheDocument()
  })

  it("keeps the profile id as a labelled tracking detail, not the identity", () => {
    renderTable()

    const row = screen.getByText("张三").closest("tr")!
    expect(within(row).getByText("辅助追踪")).toBeInTheDocument()
    expect(within(row).getByText("11111111")).toBeInTheDocument()
  })

  it("links the name to the candidate view under the run's job", () => {
    renderTable({ jobId: "44444444-4444-4444-8444-444444444444" })

    expect(screen.getByRole("link", { name: "张三" })).toHaveAttribute(
      "href",
      `/candidates/${completed.candidate_profile_id}?job=44444444-4444-4444-8444-444444444444`,
    )
  })

  it("states the fallback instead of printing an id when the profile is unreadable", () => {
    renderTable({
      candidates: [
        {
          ...completed,
          display_name: null,
          normalized_skills: [],
          years_experience: null,
          education_level: null,
        },
      ],
    })

    const row = screen.getByText("未命名候选人").closest("tr")!
    // Nothing is known, so no summary line is invented — an empty "—" next to a
    // name reads like a field that failed to load.
    expect(within(row).queryByText(/年经验/)).toBeNull()
    expect(within(row).getByText("辅助追踪")).toBeInTheDocument()
  })

  it("says how many skills were left out of the summary", () => {
    renderTable({
      candidates: [{ ...completed, normalized_skills: ["a", "b", "c", "d", "e", "f"] }],
    })

    expect(screen.getByText("8 年经验 · 硕士 · a、b、c、d 等 6 项")).toBeInTheDocument()
  })

  it("guides recovery when nothing was recalled", () => {
    renderTable({ candidates: [] })

    expect(screen.getByText("本次分析没有命中候选人")).toBeInTheDocument()
    expect(screen.getByText(/只有已确认的资料参与召回/)).toBeInTheDocument()
  })
})
