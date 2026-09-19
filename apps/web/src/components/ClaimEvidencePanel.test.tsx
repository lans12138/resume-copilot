import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"
import { ClaimEvidencePanel } from "./ClaimEvidencePanel"
import type { ClaimOut, ReportList } from "../api/types"

function claim(overrides: Partial<ClaimOut> = {}): ClaimOut {
  return {
    claim_type: "hard_rule:years_experience",
    claim_text: "满足years_experience（观测值 5.0，要求 3.0）",
    source: "RULE",
    impact_level: "MEDIUM",
    support_level: "SUPPORTED",
    confidence_note: "result=PASS; evidence=located",
    display_order: 1,
    evidences: [
      {
        evidence_chunk_id: "chunk-1",
        quote_text: "5 年",
        quote_start: 0,
        quote_end: 3,
      },
    ],
    ...overrides,
  }
}

function reports(claims: ClaimOut[]): ReportList {
  return {
    reports: [
      {
        id: "report-1",
        run_id: "run-1",
        application_id: "app-1",
        candidate_profile_id: "11111111-2222-3333-4444-555555555555",
        overall_score: 90,
        recommendation: "STRONG_MATCH",
        summary: "检索排序换算分 90.00（满分 100；…不代表模型置信度）；硬性条件结论：PASS。",
        model_snapshot_json: {},
        created_at: "2026-09-18T00:00:00Z",
        claims,
      },
    ],
  }
}

describe("ClaimEvidencePanel", () => {
  it("labels the score as a ranking conversion, not model confidence", () => {
    // The real summary repeats the score, so query the heading specifically
    // rather than by text alone.
    const { container } = render(<ClaimEvidencePanel reports={reports([claim()])} />)
    const score = container.querySelector(".section-heading > span")
    expect(score?.textContent).toContain("检索排序换算分 90.00")
    expect(score?.textContent).toContain("非模型置信度")
  })

  it("renders the verbatim excerpt of a supported claim", () => {
    render(<ClaimEvidencePanel reports={reports([claim()])} />)
    expect(screen.getByText(/5 年/)).toBeTruthy()
    expect(screen.queryByText(/支持等级已相应下调/)).toBeNull()
  })

  it("explains a claim that could not be pinned to an excerpt", () => {
    render(
      <ClaimEvidencePanel
        reports={reports([
          claim({
            support_level: "PARTIAL",
            confidence_note: "result=PASS; evidence=not_found",
            evidences: [],
          }),
        ])}
      />,
    )
    expect(screen.getByText(/未在候选人原文中定位到支持该结论的片段/)).toBeTruthy()
    expect(screen.getByText("部分支持")).toBeTruthy()
  })

  it("renders several excerpts from the same chunk without collapsing them", () => {
    // PORT-002: one claim may cite more than one range of the same chunk, so the
    // React key must include the range — keying on the chunk id alone duplicates.
    render(
      <ClaimEvidencePanel
        reports={reports([
          claim({
            evidences: [
              { evidence_chunk_id: "chunk-1", quote_text: "5 年", quote_start: 0, quote_end: 3 },
              { evidence_chunk_id: "chunk-1", quote_text: "3 年", quote_start: 9, quote_end: 12 },
            ],
          }),
        ])}
      />,
    )
    expect(screen.getAllByRole("blockquote")).toHaveLength(2)
  })

  it("orders claims by display_order so the aggregate verdict comes first", () => {
    const aggregate = claim({
      claim_type: "hard_rule_overall",
      claim_text: "候选人满足全部硬性条件，可进入下一轮",
      display_order: 0,
    })
    const rule = claim({ claim_text: "满足required_education（观测值 本科，要求 本科）" })
    const { container } = render(<ClaimEvidencePanel reports={reports([rule, aggregate])} />)
    const heads = Array.from(container.querySelectorAll(".claim-head strong"))
    expect(heads.map((el) => el.textContent)).toEqual([
      "候选人满足全部硬性条件，可进入下一轮",
      "满足required_education（观测值 本科，要求 本科）",
    ])
  })

  it("says so when the run has produced no reports yet", () => {
    render(<ClaimEvidencePanel reports={{ reports: [] }} />)
    expect(screen.getByText(/尚未生成证据化报告/)).toBeTruthy()
  })

  it("names a rule claim as a rule verdict", () => {
    render(<ClaimEvidencePanel reports={reports([claim()])} />)
    expect(screen.getByText("规则判定")).toBeTruthy()
    expect(screen.queryByText("模型解释")).toBeNull()
  })

  it("marks a model claim and says why it is capped at partial support", () => {
    // PORT-003: BR-001/BR-002 — a reader who cannot tell model commentary from a
    // deterministic verdict will read the model's wording as the decision.
    render(
      <ClaimEvidencePanel
        reports={reports([
          claim({
            claim_type: "model_conclusion",
            claim_text: "岗位要求的 Python 可在候选人原文中定位到。",
            source: "MODEL",
            support_level: "PARTIAL",
            confidence_note: "source=model; citations=1; semantic support not evaluated",
          }),
        ])}
      />,
    )
    expect(screen.getByText("模型解释")).toBeTruthy()
    expect(screen.queryByText("规则判定")).toBeNull()
    expect(screen.getByText(/引用已通过服务端校验/)).toBeTruthy()
  })

  it("does not show the model caveat on a rule claim", () => {
    render(<ClaimEvidencePanel reports={reports([claim()])} />)
    expect(screen.queryByText(/语义支持未经人工评测认定/)).toBeNull()
  })
})
