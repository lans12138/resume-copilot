import { useEffect, useRef, useState } from "react"
import type { DocumentContent, EvidenceLocator, ParsedBlockView } from "../api/types"
import { buildQuoteLocator, groupBlocks, locatorMatches, splitByQuotedRange } from "../lib/locator"
import { selectionOffsetsWithin } from "../lib/sourceSelection"

export const PIN_SECTION_TYPES: readonly string[] = ["工作经历", "项目经验", "技能", "教育背景"]

export interface PinRequest {
  locator: EvidenceLocator
  text: string
  sectionType: string
}

interface Selection {
  blockIndex: number
  start: number
  length: number
}

function BlockText({ block, active }: { block: ParsedBlockView; active: EvidenceLocator | null }) {
  // A quote that does not fit the block (stale or cross-document) is shown as
  // plain text: asserting a position the source does not have would be worse than
  // showing none.
  const parts = active && locatorMatches(block.locator, active) ? splitByQuotedRange(block.text, active) : null
  if (!parts) return <>{block.text}</>
  return (
    <>
      {parts.before}
      <mark className="quote">{parts.quoted}</mark>
      {parts.after}
    </>
  )
}

/**
 * Original-source viewer for the review screen (FIN-004).
 *
 * Two jobs, deliberately separated: render the parsed source grouped the way the
 * reader sees it (pages / paragraphs / tables) and let the reviewer turn an
 * excerpt into evidence. A pin never guesses — it needs an actual selection
 * inside one block, or the whole-block button, and both produce a locator whose
 * char range is checked against the block text before anything is submitted.
 */
export function SourceLocatorViewer({
  content,
  activeLocator,
  evidenceLocators,
  onPin,
  pinning = false,
  pinDisabled = false,
}: {
  content: DocumentContent
  activeLocator: EvidenceLocator | null
  evidenceLocators: EvidenceLocator[]
  onPin: (request: PinRequest) => void
  pinning?: boolean
  pinDisabled?: boolean
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const blockRefs = useRef(new Map<number, HTMLParagraphElement>())
  const [selection, setSelection] = useState<Selection | null>(null)
  const [sectionType, setSectionType] = useState(PIN_SECTION_TYPES[0])

  // Locating evidence must move the reviewer's focus, not just the scroll
  // position: the block is focused so the excerpt can be read with the keyboard.
  useEffect(() => {
    if (!activeLocator) return
    const index = content.blocks.findIndex((block) => locatorMatches(block.locator, activeLocator))
    if (index === -1) return
    const element = blockRefs.current.get(index)
    if (!element) return
    element.scrollIntoView({ block: "center" })
    element.focus()
  }, [activeLocator, content.blocks])

  function clearSelection() {
    window.getSelection()?.removeAllRanges()
    setSelection(null)
  }

  function readSelection(target: EventTarget | null) {
    const element = (target as HTMLElement | null)?.closest("[data-block-index]") as HTMLElement | null
    if (!element || !containerRef.current?.contains(element)) {
      setSelection(null)
      return
    }
    const offsets = selectionOffsetsWithin(element)
    if (!offsets) {
      setSelection(null)
      return
    }
    setSelection({ blockIndex: Number(element.dataset.blockIndex), start: offsets.start, length: offsets.length })
  }

  const selectedBlock = selection ? content.blocks[selection.blockIndex] : undefined
  const pendingLocator = selectedBlock && selection
    ? buildQuoteLocator(selectedBlock, selection.start, selection.length)
    : null

  function pin(request: PinRequest) {
    clearSelection()
    onPin(request)
  }

  const groups = groupBlocks(content.blocks)

  return (
    <section className="panel source-panel" ref={containerRef} aria-label="简历原文">
      <div className="section-heading">
        <h2>简历原文</h2>
        <span>
          {content.page_count !== null ? `${content.page_count} 页 · ` : ""}
          {content.page_count === null && content.paragraph_count !== null ? `${content.paragraph_count} 段 · ` : ""}
          共 {content.blocks.length} 个可定位块
        </span>
      </div>

      {content.warnings.length > 0 ? (
        <div className="notice" role="status">
          <strong>解析提示</strong>
          <span>{content.warnings.join("；")}</span>
        </div>
      ) : null}

      {pendingLocator ? (
        <div className="pin-bar" role="group" aria-label="钉住所选证据">
          <span>
            已选 {selection?.length} 个字符：「{selectedBlock?.text.slice(selection?.start ?? 0, (selection?.start ?? 0) + (selection?.length ?? 0))}」
          </span>
          <label htmlFor="pin-section">证据类别</label>
          <select
            id="pin-section"
            value={sectionType}
            onChange={(event) => setSectionType(event.target.value)}
          >
            {PIN_SECTION_TYPES.map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
          <button
            type="button"
            className="button button-primary button-small"
            disabled={pinning || pinDisabled}
            onClick={() => pin({ locator: pendingLocator, text: selectedBlock!.text.slice(selection!.start, selection!.start + selection!.length), sectionType })}
          >
            {pinning ? "钉住中…" : "钉住所选片段"}
          </button>
          <button type="button" className="button button-ghost button-small" onClick={clearSelection}>取消选择</button>
        </div>
      ) : (
        <p className="muted">用鼠标选中原文中的片段即可钉为证据；也可以直接钉住整块。</p>
      )}

      <div className="source-body" onMouseUp={(event) => readSelection(event.target)} onKeyUp={(event) => readSelection(event.target)}>
        {groups.map((group) => (
          <section key={group.key} className="source-group">
            <h3 className="source-group-title">{group.label}</h3>
            {group.blocks.map(({ block, index }) => {
              const pinned = evidenceLocators.filter((locator) => locatorMatches(block.locator, locator)).length
              const isActive = activeLocator !== null && locatorMatches(block.locator, activeLocator)
              return (
                <p
                  key={`${group.key}-${index}`}
                  className={`source-block${isActive ? " is-active" : ""}${pinned > 0 ? " has-evidence" : ""}`}
                  data-block-index={index}
                  data-has-evidence={pinned}
                  tabIndex={-1}
                  ref={(element) => {
                    if (element) blockRefs.current.set(index, element)
                    else blockRefs.current.delete(index)
                  }}
                >
                  <span className="source-text"><BlockText block={block} active={activeLocator} /></span>
                  <span className="source-block-actions">
                    {pinned > 0 ? <span className="badge badge-info">已钉 {pinned} 条证据</span> : null}
                    <button
                      type="button"
                      className="button button-ghost button-small"
                      disabled={pinning || pinDisabled}
                      onClick={() => {
                        const locator = buildQuoteLocator(block, 0, block.text.length)
                        if (locator) pin({ locator, text: block.text, sectionType })
                      }}
                    >
                      钉住本块
                    </button>
                  </span>
                </p>
              )
            })}
          </section>
        ))}
      </div>
    </section>
  )
}
