import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { SourceLocatorViewer } from "./SourceLocatorViewer"
import type { DocumentContent, EvidenceLocator } from "../api/types"

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const pdfContent: DocumentContent = {
  document_id: "11111111-1111-4111-8111-111111111111",
  media_type: "application/pdf",
  full_text: "张伟\n6 年 Python 后端开发经验\n联系方式",
  page_count: 2,
  paragraph_count: null,
  table_count: null,
  warnings: [],
  blocks: [
    { block_index: 0, text: "张伟", locator: { kind: "pdf", page_number: 1, block_index: 0, char_start: 0, char_end: 2 } },
    {
      block_index: 1,
      text: "6 年 Python 后端开发经验",
      locator: { kind: "pdf", page_number: 1, block_index: 1, char_start: 0, char_end: 17 },
    },
    { block_index: 2, text: "联系方式", locator: { kind: "pdf", page_number: 2, block_index: 0, char_start: 0, char_end: 4 } },
  ],
}

const docxContent: DocumentContent = {
  document_id: "22222222-2222-4222-8222-222222222222",
  media_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  full_text: "教育背景\n2019 - 2023\n本科",
  page_count: null,
  paragraph_count: 1,
  table_count: 1,
  warnings: [],
  blocks: [
    {
      block_index: 0,
      text: "教育背景",
      locator: { kind: "docx_paragraph", paragraph_index: 0, char_start: 0, char_end: 4 },
    },
    {
      block_index: 1,
      text: "2019 - 2023 本科",
      locator: { kind: "docx_table", table_index: 0, row_index: 0, cell_index: 0, char_start: 0, char_end: 14 },
    },
  ],
}

/** Point the viewer's selection reader at a range inside one block. */
function selectInside(blockElement: HTMLElement, start: number, end: number) {
  const textNode = blockElement.querySelector(".source-text")!.firstChild as Text
  const range = document.createRange()
  range.setStart(textNode, start)
  range.setEnd(textNode, end)
  vi.spyOn(window, "getSelection").mockReturnValue({
    rangeCount: 1,
    isCollapsed: false,
    getRangeAt: () => range,
    removeAllRanges: () => {},
  } as unknown as Selection)
}

describe("SourceLocatorViewer", () => {
  it("groups PDF blocks by page and shows the quoted range of the located evidence", () => {
    // "6 年 Python 后端开发经验" — the quote covers "Python 后端" (chars 4–13).
    const active: EvidenceLocator = { kind: "pdf", page_number: 1, block_index: 1, char_start: 4, char_end: 13 }
    render(
      <SourceLocatorViewer
        content={pdfContent}
        activeLocator={active}
        evidenceLocators={[active]}
        onPin={() => {}}
      />,
    )

    expect(screen.getByText("第 1 页")).toBeInTheDocument()
    expect(screen.getByText("第 2 页")).toBeInTheDocument()

    // Only the quoted sub-range is marked; the rest of the block stays readable.
    const mark = screen.getByText("Python 后端")
    expect(mark.tagName).toBe("MARK")
    expect(mark.closest("[data-block-index]")).toHaveAttribute("data-block-index", "1")
    expect(screen.getByText("已钉 1 条证据")).toBeInTheDocument()
  })

  it("moves focus to the block an evidence chunk points at", () => {
    const active: EvidenceLocator = { kind: "pdf", page_number: 2, block_index: 0, char_start: 0, char_end: 4 }
    render(
      <SourceLocatorViewer content={pdfContent} activeLocator={active} evidenceLocators={[]} onPin={() => {}} />,
    )

    expect(document.activeElement).toBe(document.querySelector('[data-block-index="2"]'))
  })

  it("groups DOCX paragraphs and table cells separately", () => {
    render(
      <SourceLocatorViewer content={docxContent} activeLocator={null} evidenceLocators={[]} onPin={() => {}} />,
    )

    expect(screen.getByText("段落")).toBeInTheDocument()
    expect(screen.getByText("表格 0")).toBeInTheDocument()
    expect(document.querySelector('[data-block-index="1"]')).toHaveTextContent("2019 - 2023 本科")
  })

  it("does not highlight when the locator points at a block the document does not have", () => {
    // A stale or cross-document locator must not be presented as a real position.
    const stale: EvidenceLocator = { kind: "pdf", page_number: 9, block_index: 0, char_start: 0, char_end: 2 }
    render(
      <SourceLocatorViewer content={pdfContent} activeLocator={stale} evidenceLocators={[]} onPin={() => {}} />,
    )

    expect(document.querySelector("mark.quote")).toBeNull()
    expect(document.querySelector('[data-block-index="1"]')).toHaveTextContent("6 年 Python 后端开发经验")
  })

  it("pins a whole block with the block's own structural coordinates", async () => {
    const onPin = vi.fn()
    render(
      <SourceLocatorViewer content={docxContent} activeLocator={null} evidenceLocators={[]} onPin={onPin} />,
    )

    await userEvent.click(screen.getAllByRole("button", { name: "钉住本块" })[1])

    expect(onPin).toHaveBeenCalledWith({
      locator: {
        kind: "docx_table", table_index: 0, row_index: 0, cell_index: 0, char_start: 0, char_end: 14,
      },
      text: "2019 - 2023 本科",
      sectionType: "工作经历",
    })
  })

  it("pins only the selected excerpt and keeps the block identity", async () => {
    const onPin = vi.fn()
    render(
      <SourceLocatorViewer content={pdfContent} activeLocator={null} evidenceLocators={[]} onPin={onPin} />,
    )

    const block = document.querySelector<HTMLElement>('[data-block-index="1"]')!
    selectInside(block, 2, 10)
    fireEvent.mouseUp(block)

    expect(screen.getByRole("group", { name: "钉住所选证据" })).toHaveTextContent("已选 8 个字符")
    await userEvent.selectOptions(screen.getByLabelText("证据类别"), "技能")
    await userEvent.click(screen.getByRole("button", { name: "钉住所选片段" }))

    expect(onPin).toHaveBeenCalledWith({
      locator: { kind: "pdf", page_number: 1, block_index: 1, char_start: 2, char_end: 10 },
      text: "年 Python",
      sectionType: "技能",
    })
  })

  it("offers no pin bar for a selection that leaves the block", () => {
    render(
      <SourceLocatorViewer content={pdfContent} activeLocator={null} evidenceLocators={[]} onPin={() => {}} />,
    )

    const first = document.querySelector<HTMLElement>('[data-block-index="0"]')!
    const second = document.querySelector<HTMLElement>('[data-block-index="1"]')!
    const range = document.createRange()
    range.setStart(first.querySelector(".source-text")!.firstChild as Text, 0)
    range.setEnd(second.querySelector(".source-text")!.firstChild as Text, 4)
    vi.spyOn(window, "getSelection").mockReturnValue({
      rangeCount: 1, isCollapsed: false, getRangeAt: () => range, removeAllRanges: () => {},
    } as unknown as Selection)

    fireEvent.mouseUp(second)

    expect(screen.queryByRole("group", { name: "钉住所选证据" })).toBeNull()
  })

  it("keeps the pin action unavailable while a write is in flight", () => {
    render(
      <SourceLocatorViewer content={pdfContent} activeLocator={null} evidenceLocators={[]} onPin={() => {}} pinning />,
    )

    for (const button of screen.getAllByRole("button", { name: "钉住本块" })) {
      expect(button).toBeDisabled()
    }
  })
})
