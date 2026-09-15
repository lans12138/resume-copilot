import { afterEach, describe, expect, it, vi } from "vitest"
import { selectionOffsetsWithin } from "./sourceSelection"

function blockWithHtml(html: string): HTMLElement {
  const element = document.createElement("p")
  element.innerHTML = html
  document.body.append(element)
  return element
}

/** Stand in for the browser selection; only the members we read are needed. */
function stubSelection(range: Range | null): void {
  vi.spyOn(window, "getSelection").mockReturnValue(
    (range
      ? { rangeCount: 1, isCollapsed: false, getRangeAt: () => range }
      : { rangeCount: 0, isCollapsed: true, getRangeAt: () => null }) as unknown as Selection,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  document.body.innerHTML = ""
})

describe("selectionOffsetsWithin", () => {
  it("measures a selection as character offsets into the block text", () => {
    const element = blockWithHtml("6 年 Python 后端开发经验")
    const text = element.firstChild as Text
    const range = document.createRange()
    range.setStart(text, 4)
    range.setEnd(text, 10)
    stubSelection(range)

    expect(selectionOffsetsWithin(element)).toEqual({ start: 4, length: 6 })
  })

  it("counts offsets across rendered nodes, not just the selected one", () => {
    // A highlighted excerpt splits the block into several text nodes; a node-local
    // offset would report position 2 instead of 8 and pin the wrong characters.
    const element = blockWithHtml("前端经验<b>后端经验</b>")
    const inner = element.lastChild as HTMLElement
    const range = document.createRange()
    range.setStart(inner.firstChild as Text, 0)
    range.setEnd(inner.firstChild as Text, 2)
    stubSelection(range)

    expect(selectionOffsetsWithin(element)).toEqual({ start: 4, length: 2 })
  })

  it("rejects an empty selection", () => {
    const element = blockWithHtml("文本")
    stubSelection(null)
    expect(selectionOffsetsWithin(element)).toBeNull()
  })

  it("rejects a selection that leaves the block", () => {
    const element = blockWithHtml("第一块")
    const other = blockWithHtml("第二块")
    const range = document.createRange()
    range.setStart(element.firstChild as Text, 0)
    range.setEnd(other.firstChild as Text, 3)
    stubSelection(range)

    expect(selectionOffsetsWithin(element)).toBeNull()
  })
})
