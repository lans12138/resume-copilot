import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
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
}

describe("RankingTable", () => {
  it("starts an ApplicationRun with the durable application id", async () => {
    const onStartApplication = vi.fn()
    render(
      <RankingTable
        candidates={[completed]}
        onStartApplication={onStartApplication}
      />,
    )

    await userEvent.click(screen.getByRole("button", { name: "启动单人流程" }))

    expect(onStartApplication).toHaveBeenCalledOnce()
    expect(onStartApplication).toHaveBeenCalledWith(completed.application_id)
  })

  it("does not start a candidate whose MatchRun processing failed", () => {
    render(
      <RankingTable
        candidates={[{ ...completed, processing_status: "FAILED" }]}
        onStartApplication={vi.fn()}
      />,
    )

    const row = screen.getByText("失败").closest("tr")!
    expect(within(row).getByRole("button", { name: "启动单人流程" })).toBeDisabled()
  })

  it("shows which application is being started", () => {
    render(
      <RankingTable
        candidates={[completed]}
        onStartApplication={vi.fn()}
        startingApplicationId={completed.application_id}
      />,
    )

    expect(screen.getByRole("button", { name: "启动中…" })).toBeDisabled()
  })
})
