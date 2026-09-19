import { describe, expect, it } from "vitest"
import {
  UNNAMED_CANDIDATE,
  candidateFacts,
  candidateName,
  educationLabel,
  shortProfileId,
} from "./candidateDisplay"

describe("candidateName", () => {
  it("uses the profile's name", () => {
    expect(candidateName("张三")).toBe("张三")
  })

  it("states a fallback instead of rendering an empty cell or an id", () => {
    expect(candidateName(null)).toBe(UNNAMED_CANDIDATE)
    expect(candidateName(undefined)).toBe(UNNAMED_CANDIDATE)
    expect(candidateName("   ")).toBe(UNNAMED_CANDIDATE)
  })
})

describe("educationLabel", () => {
  it("translates the stored enum", () => {
    expect(educationLabel("MASTER")).toBe("硕士")
    expect(educationLabel("BACHELOR")).toBe("本科")
  })

  it("passes an unknown value through rather than hiding it", () => {
    // A value this build does not know is still information; dropping it would
    // make the summary claim less than the profile says.
    expect(educationLabel("POSTDOC")).toBe("POSTDOC")
  })

  it("treats an absent level as unknown, not as a label", () => {
    expect(educationLabel(null)).toBeNull()
    expect(educationLabel(undefined)).toBeNull()
    expect(educationLabel("")).toBeNull()
  })
})

describe("candidateFacts", () => {
  it("reads in order: experience, education, skills", () => {
    expect(
      candidateFacts({
        years_experience: 8,
        education_level: "MASTER",
        normalized_skills: ["python", "go"],
      }),
    ).toEqual(["8 年经验", "硕士", "python、go"])
  })

  it("keeps a fractional experience count readable", () => {
    expect(candidateFacts({ years_experience: 8.5 })).toEqual(["8.5 年经验"])
  })

  it("does not turn a missing experience count into zero years", () => {
    // 0 年经验 is a fact about the candidate; null is the absence of one.
    expect(candidateFacts({ years_experience: null })).toEqual([])
  })

  it("says how many skills were left out", () => {
    expect(candidateFacts({ normalized_skills: ["a", "b", "c", "d", "e", "f"] })).toEqual([
      "a、b、c、d 等 6 项",
    ])
  })

  it("does not claim a truncation when nothing was truncated", () => {
    expect(candidateFacts({ normalized_skills: ["a", "b"] })).toEqual(["a、b"])
  })

  it("ignores blank skills", () => {
    expect(candidateFacts({ normalized_skills: ["python", "  ", ""] })).toEqual(["python"])
  })

  it("returns nothing when nothing is known", () => {
    expect(candidateFacts({})).toEqual([])
  })
})

describe("shortProfileId", () => {
  it("is the tracking form, not the identity", () => {
    expect(shortProfileId("11111111-2222-3333-4444-555555555555")).toBe("11111111")
  })
})
