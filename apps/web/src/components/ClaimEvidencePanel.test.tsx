import { describe, expect, it } from "vitest"
import { render as rtlRender, screen } from "@testing-library/react"
import type { ReactElement } from "react"
import { MemoryRouter } from "react-router-dom"
import { ClaimEvidencePanel } from "./ClaimEvidencePanel"
import type { ClaimOut, ReportList } from "../api/types"

// PORT-005: the panel now links each excerpt to its source, so every case needs a
// router. Shadowing `render` keeps the existing call sites unchanged.
const render = (ui: ReactElement) => rtlRender(<MemoryRouter>{ui}</MemoryRouter>)

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
        locator_json: { kind: "docx_paragraph", paragraph_index: 12, char_start: 4, char_end: 7 },
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
        display_name: "张三",
        document_id: "doc-1",
      },
    ],
  }
}

describe("ClaimEvidencePanel", () => {
  it("titles the report with the candidate's name", () => {
    render(<ClaimEvidencePanel reports={reports([claim()])} />)

    expect(screen.getByRole("heading", { name: "候选人 张三" })).toBeInTheDocument()
    expect(screen.getByText("辅助追踪")).toBeInTheDocument()
  })

  it("falls back to a stated label when the profile cannot be read", () => {
    const list = reports([claim()])
    list.reports[0].display_name = null
    render(<ClaimEvidencePanel reports={list} />)

    expect(screen.getByRole("heading", { name: "候选人 未命名候选人" })).toBeInTheDocument()
  })

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
              {
                evidence_chunk_id: "chunk-1",
                quote_text: "5 年",
                quote_start: 0,
                quote_end: 3,
                locator_json: null,
              },
              {
                evidence_chunk_id: "chunk-1",
                quote_text: "3 年",
                quote_start: 9,
                quote_end: 12,
                locator_json: null,
              },
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

  // PORT-005: reading a quote and then hunting for it across the documents list was
  // the "手工切换多页寻找原文" the roadmap calls out.
  it("states where each excerpt came from", () => {
    render(<ClaimEvidencePanel reports={reports([claim()])} />)

    expect(screen.getByText("第 12 段")).toBeInTheDocument()
  })

  it("opens the excerpt in its source document, already located", () => {
    render(<ClaimEvidencePanel reports={reports([claim()])} />)

    expect(screen.getByRole("link", { name: "查看原文 →" })).toHaveAttribute(
      "href",
      "/documents/doc-1/review?chunk=chunk-1",
    )
  })

  it("renders an unreadable locator as unknown rather than dropping the excerpt", () => {
    // The column is dict[str, Any]: the demo seed writes its own shape, so a
    // locator this build cannot parse is an expected input, not a corrupt response.
    render(
      <ClaimEvidencePanel
        reports={reports([
          claim({
            evidences: [
              {
                evidence_chunk_id: "chunk-9",
                quote_text: "5 年",
                quote_start: 0,
                quote_end: 3,
                locator_json: { page: 3, bbox: [0, 0, 1, 1] },
              },
            ],
          }),
        ])}
      />,
    )

    expect(screen.getByText("位置未知")).toBeInTheDocument()
    // Still openable: the chunk id is what the review screen locates by.
    expect(screen.getByRole("link", { name: "查看原文 →" })).toBeInTheDocument()
  })

  it("says why the source cannot be opened when the document is unknown", () => {
    const list = reports([claim()])
    list.reports[0].document_id = null
    render(<ClaimEvidencePanel reports={list} />)

    expect(screen.queryByRole("link", { name: "查看原文 →" })).toBeNull()
    expect(screen.getByText(/来源文档不可用/)).toBeInTheDocument()
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

  // PORT-005: 「标明结果来源」 — a reader should know before scrolling whether any of
  // the report is model commentary, rather than inferring it from the badges.
  it("summarises the mix of rule verdicts and model commentary", () => {
    render(
      <ClaimEvidencePanel
        reports={reports([
          claim(),
          // display_order is unique per report in the database
          // (uq_report_claims_order), so the fixture must not reuse one.
          claim({ claim_type: "model_conclusion", claim_text: "候选人经验与岗位描述接近。", source: "MODEL", support_level: "PARTIAL", display_order: 2 }),
        ])}
      />,
    )

    expect(screen.getByText("结果来源：规则判定 1 条 · 模型解释 1 条。")).toBeInTheDocument()
  })

  it("does not claim a model contribution a rule-only report does not have", () => {
    render(<ClaimEvidencePanel reports={reports([claim()])} />)

    expect(screen.getByText("结果来源：规则判定 1 条。")).toBeInTheDocument()
  })

  it("says a report is empty rather than showing an empty provenance line", () => {
    render(<ClaimEvidencePanel reports={reports([])} />)

    expect(screen.getByText("本报告没有结论。")).toBeInTheDocument()
  })
})
