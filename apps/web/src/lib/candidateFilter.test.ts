import { describe, expect, it } from "vitest"
import { applyCandidateFilter } from "./candidateFilter"
import type { CandidateRanking } from "../api/types"

const items: CandidateRanking[] = [
  {
    candidate_profile_id: "a", snapshot_order: 1, rrf_score: 0.5, display_name: "A",
    normalized_skills: ["python"], years_experience: 5, education_level: "MASTER",
    structured_rank: 1, structured_score: 0.9, keyword_rank: 2, keyword_score: 0.8,
    vector_rank: null, vector_score: null, hard_rule: { rules: [], overall: "PASS" },
  },
  {
    candidate_profile_id: "b", snapshot_order: 2, rrf_score: 0.3, display_name: "B",
    normalized_skills: ["java"], years_experience: 1, education_level: "HIGH_SCHOOL",
    structured_rank: 3, structured_score: 0.4, keyword_rank: null, keyword_score: null,
    vector_rank: null, vector_score: null, hard_rule: { rules: [], overall: "FAIL" },
  },
  {
    candidate_profile_id: "c", snapshot_order: 3, rrf_score: 0.1, display_name: "C",
    normalized_skills: [], years_experience: null, education_level: null,
    structured_rank: null, structured_score: null, keyword_rank: null, keyword_score: null,
    vector_rank: 5, vector_score: 0.2, hard_rule: { rules: [], overall: "UNKNOWN" },
  },
]

describe("applyCandidateFilter", () => {
  it("returns every candidate when no filter is set", () => {
    expect(applyCandidateFilter(items, {}).length).toBe(3)
  })

  it("filters by hard rule and never mutates the source list", () => {
    const originalLength = items.length
    const pass = applyCandidateFilter(items, { hard_rule: "PASS" })
    expect(pass.map((item) => item.candidate_profile_id)).toEqual(["a"])
    // The source array is untouched: filtering only narrows the display, it
    // cannot shrink the MatchRun candidate set (detailed design §15.4).
    expect(items.length).toBe(originalLength)
  })

  it("filters to candidates hit by a given channel", () => {
    const vector = applyCandidateFilter(items, { channel: "vector" })
    expect(vector.map((item) => item.candidate_profile_id)).toEqual(["c"])
  })

  it("treats ALL as no-op for both dimensions", () => {
    expect(applyCandidateFilter(items, { hard_rule: "ALL", channel: "ALL" }).length).toBe(3)
  })
})
