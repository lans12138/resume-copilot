import { describe, expect, it } from "vitest"
import { render, screen, within } from "@testing-library/react"
import { CandidateTable } from "./CandidateTable"
import type { CandidateRanking } from "../api/types"

const items: CandidateRanking[] = [
  {
    candidate_profile_id: "a", snapshot_order: 1, rrf_score: 0.5, display_name: "Alice",
    normalized_skills: ["python"], years_experience: 5, education_level: "MASTER",
    structured_rank: 1, structured_score: 0.9, keyword_rank: 2, keyword_score: 0.8,
    vector_rank: 3, vector_score: 0.5, hard_rule: { rules: [], overall: "PASS" },
  },
]

describe("CandidateTable", () => {
  it("renders one row per candidate with per-channel cells and hard rule", () => {
    render(<CandidateTable items={items} />)
    const row = screen.getByText("Alice").closest("tr")!
    expect(within(row).getByText("#1 · 0.900")).toBeInTheDocument()
    expect(within(row).getByText("#2 · 0.800")).toBeInTheDocument()
    expect(within(row).getByText("#3 · 0.500")).toBeInTheDocument()
    expect(within(row).getByText("通过")).toBeInTheDocument()
  })

  it("shows a dash for a channel that did not rank the candidate", () => {
    const noHit: CandidateRanking = { ...items[0], structured_rank: null, structured_score: null }
    render(<CandidateTable items={[noHit]} />)
    expect(screen.getByText("—")).toBeInTheDocument()
  })
})
