import { describe, expect, it } from "vitest"
import {
  buildQuoteLocator,
  describeLocator,
  findBlockIndex,
  groupBlocks,
  locatorCharRange,
  locatorMatches,
  parseLocator,
  splitByQuotedRange,
} from "./locator"
import type { EvidenceLocator, ParsedBlockView } from "../api/types"

const paragraph: EvidenceLocator = { kind: "docx_paragraph", paragraph_index: 2, char_start: 0, char_end: 13 }
const pdf: EvidenceLocator = { kind: "pdf", page_number: 1, block_index: 0, char_start: 0, char_end: 2 }
const table: EvidenceLocator = {
  kind: "docx_table", table_index: 0, row_index: 1, cell_index: 2, char_start: 4, char_end: 9,
}

describe("locatorMatches", () => {
  it("ignores the quoted range so a mid-paragraph quote still matches its block", () => {
    const midQuote: EvidenceLocator = { ...paragraph, char_start: 5, char_end: 9 }
    expect(locatorMatches(paragraph, midQuote)).toBe(true)
  })

  it("does not match across locator kinds", () => {
    expect(locatorMatches(paragraph, pdf)).toBe(false)
  })

  it("separates different paragraphs, pages and table cells", () => {
    expect(locatorMatches(paragraph, { ...paragraph, paragraph_index: 3 })).toBe(false)
    expect(locatorMatches(pdf, { ...pdf, page_number: 2 })).toBe(false)
    expect(locatorMatches(table, { ...table, row_index: 2 })).toBe(false)
    expect(locatorMatches(table, { ...table, cell_index: 0 })).toBe(false)
  })
})

describe("locatorCharRange", () => {
  it("returns a usable range", () => {
    expect(locatorCharRange(paragraph)).toEqual({ start: 0, end: 13 })
  })

  it("rejects an empty or inverted range", () => {
    expect(locatorCharRange({ ...paragraph, char_start: 5, char_end: 5 })).toBeNull()
    expect(locatorCharRange({ ...paragraph, char_start: 9, char_end: 4 })).toBeNull()
    expect(locatorCharRange({ ...paragraph, char_start: -1, char_end: 4 })).toBeNull()
  })
})

describe("describeLocator", () => {
  it("describes each locator kind", () => {
    expect(describeLocator(pdf)).toBe("第 1 页 · 第 0 块 · 字符 0–2")
    expect(describeLocator(paragraph)).toBe("第 2 段 · 字符 0–13")
    expect(describeLocator(table)).toBe("表格 0 · 第 1 行 · 第 2 列 · 字符 4–9")
  })

  it("says so when the range is unusable instead of printing nonsense", () => {
    expect(describeLocator({ ...paragraph, char_start: 3, char_end: 3 })).toContain("字符范围未知")
  })
})

describe("splitByQuotedRange", () => {
  it("splits the block around the quoted excerpt", () => {
    const text = "6 年 Python 后端开发经验，熟悉 FastAPI。"
    const locator: EvidenceLocator = { ...paragraph, char_start: 0, char_end: 13 }
    expect(splitByQuotedRange(text, locator)).toEqual({
      before: "",
      quoted: "6 年 Python 后端",
      after: "开发经验，熟悉 FastAPI。",
    })
  })

  it("returns null when the range does not fit the block", () => {
    // A stale or cross-document locator must not be presented as a position that
    // the text does not actually have.
    expect(splitByQuotedRange("短文本", { ...paragraph, char_start: 0, char_end: 99 })).toBeNull()
    expect(splitByQuotedRange("短文本", { ...paragraph, char_start: 0, char_end: 0 })).toBeNull()
  })
})

describe("findBlockIndex", () => {
  const blocks: ParsedBlockView[] = [
    { block_index: 0, text: "张伟", locator: pdf },
    { block_index: 1, text: "6 年 Python 后端开发经验", locator: paragraph },
  ]

  it("locates the block a locator points at", () => {
    expect(findBlockIndex(blocks, paragraph)).toBe(1)
  })

  it("returns null for a missing locator or an unknown position", () => {
    expect(findBlockIndex(blocks, null)).toBeNull()
    expect(findBlockIndex(blocks, { ...paragraph, paragraph_index: 99 })).toBeNull()
  })
})

describe("parseLocator", () => {
  it("narrows every locator kind the parser produces", () => {
    expect(parseLocator(pdf)).toEqual(pdf)
    expect(parseLocator(paragraph)).toEqual(paragraph)
    expect(parseLocator(table)).toEqual(table)
  })

  it("rejects a foreign locator shape instead of inventing a position", () => {
    // The demo seed writes {"page", "bbox"} straight into the JSON column, and the
    // API response type is `dict[str, Any]`; a chunk like that must degrade to
    // "位置未知" rather than crash the evidence panel.
    expect(parseLocator({ page: 1, bbox: [0, 0, 100, 20] })).toBeNull()
    expect(parseLocator(null)).toBeNull()
    expect(parseLocator("pdf")).toBeNull()
  })

  it("rejects a structurally incomplete locator", () => {
    expect(parseLocator({ kind: "pdf", page_number: 1, char_start: 0, char_end: 2 })).toBeNull()
    expect(parseLocator({ kind: "docx_table", table_index: 0, row_index: 1, char_start: 0, char_end: 2 })).toBeNull()
    expect(parseLocator({ kind: "docx_paragraph", paragraph_index: 2, char_start: "0", char_end: 2 })).toBeNull()
  })

  it("keeps an unusable char range out of the happy path", () => {
    // Structural validity and range usability are separate questions: the range is
    // rejected later by locatorCharRange so the caller can still show the position.
    const narrowed = parseLocator({ ...paragraph, char_start: 5, char_end: 5 })
    expect(narrowed).not.toBeNull()
    expect(locatorCharRange(narrowed!)).toBeNull()
  })
})

describe("buildQuoteLocator", () => {
  const block: ParsedBlockView = {
    block_index: 1,
    text: "6 年 Python 后端开发经验，熟悉 FastAPI。",
    locator: { ...paragraph, char_start: 0, char_end: 20 },
  }

  it("keeps the block identity and narrows only the char range", () => {
    expect(buildQuoteLocator(block, 2, 15)).toEqual({ ...paragraph, char_start: 2, char_end: 17 })
  })

  it("refuses a selection that does not fit the block", () => {
    expect(buildQuoteLocator(block, 0, 0)).toBeNull()
    expect(buildQuoteLocator(block, 5, 99)).toBeNull()
    expect(buildQuoteLocator(block, -1, 4)).toBeNull()
    expect(buildQuoteLocator(block, 1.5, 4)).toBeNull()
  })

  it("accepts the whole block as an excerpt", () => {
    const whole = buildQuoteLocator(block, 0, block.text.length)
    expect(whole && locatorCharRange(whole)).toEqual({ start: 0, end: block.text.length })
  })
})

describe("groupBlocks", () => {
  it("groups PDF blocks by page using the locator's own page number", () => {
    const blocks: ParsedBlockView[] = [
      { block_index: 0, text: "张伟", locator: { kind: "pdf", page_number: 1, block_index: 0, char_start: 0, char_end: 2 } },
      { block_index: 1, text: "联系方式", locator: { kind: "pdf", page_number: 1, block_index: 1, char_start: 0, char_end: 4 } },
      { block_index: 2, text: "项目经历", locator: { kind: "pdf", page_number: 3, block_index: 0, char_start: 0, char_end: 4 } },
    ]
    const groups = groupBlocks(blocks)
    expect(groups.map((group) => group.label)).toEqual(["第 1 页", "第 3 页"])
    expect(groups[0].blocks).toHaveLength(2)
  })

  it("keeps paragraphs in one run and splits tables by table index", () => {
    const blocks: ParsedBlockView[] = [
      { block_index: 0, text: "教育背景", locator: { kind: "docx_paragraph", paragraph_index: 0, char_start: 0, char_end: 4 } },
      { block_index: 1, text: "2019 - 2023", locator: { kind: "docx_table", table_index: 0, row_index: 0, cell_index: 0, char_start: 0, char_end: 11 } },
      { block_index: 2, text: "本科", locator: { kind: "docx_table", table_index: 0, row_index: 0, cell_index: 1, char_start: 0, char_end: 2 } },
      { block_index: 3, text: "技能", locator: { kind: "docx_paragraph", paragraph_index: 2, char_start: 0, char_end: 2 } },
    ]
    const groups = groupBlocks(blocks)
    expect(groups.map((group) => group.label)).toEqual(["段落", "表格 0", "段落"])
    expect(groups[1].blocks).toHaveLength(2)
  })
})
