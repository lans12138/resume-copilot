import type { EvidenceLocator, ParsedBlockView } from "../api/types"

/**
 * Locator helpers shared by the source viewer and the evidence preview.
 *
 * An evidence chunk points at a *sub-range* of a parsed block, so two questions
 * have to stay separate: which block does this locator belong to (structural
 * identity), and which characters inside it were quoted (the char range). Mixing
 * them would make highlighting depend on the excerpt length, and a chunk that
 * quoted the middle of a paragraph would silently fail to match.
 */

/** Structural identity: the same block, ignoring the quoted char range. */
export function locatorMatches(block: EvidenceLocator, target: EvidenceLocator): boolean {
  if (block.kind !== target.kind) return false
  switch (target.kind) {
    case "pdf":
      return block.kind === "pdf" && block.page_number === target.page_number
        && block.block_index === target.block_index
    case "docx_paragraph":
      return block.kind === "docx_paragraph" && block.paragraph_index === target.paragraph_index
    case "docx_table":
      return block.kind === "docx_table" && block.table_index === target.table_index
        && block.row_index === target.row_index && block.cell_index === target.cell_index
  }
}

function isIndex(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0
}

function isCharBound(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value)
}

/**
 * Narrow an untrusted locator (an evidence chunk's `locator_json`) to the union.
 *
 * Returns null instead of guessing. A chunk whose locator is missing, was written
 * by a different producer, or points at a kind this UI does not know still has to
 * render — the caller shows "位置未知" and leaves the source unhighlighted rather
 * than inventing a position.
 */
export function parseLocator(value: unknown): EvidenceLocator | null {
  if (typeof value !== "object" || value === null) return null
  const raw = value as Record<string, unknown>
  if (!isCharBound(raw.char_start) || !isCharBound(raw.char_end)) return null
  const { char_start: charStart, char_end: charEnd } = raw
  switch (raw.kind) {
    case "pdf":
      if (!isIndex(raw.page_number) || !isIndex(raw.block_index)) return null
      return {
        kind: "pdf", page_number: raw.page_number, block_index: raw.block_index,
        char_start: charStart, char_end: charEnd,
      }
    case "docx_paragraph":
      if (!isIndex(raw.paragraph_index)) return null
      return { kind: "docx_paragraph", paragraph_index: raw.paragraph_index, char_start: charStart, char_end: charEnd }
    case "docx_table":
      if (!isIndex(raw.table_index) || !isIndex(raw.row_index) || !isIndex(raw.cell_index)) return null
      return {
        kind: "docx_table", table_index: raw.table_index, row_index: raw.row_index,
        cell_index: raw.cell_index, char_start: charStart, char_end: charEnd,
      }
    default:
      return null
  }
}

/**
 * Build the locator for an excerpt a reviewer selected inside a block.
 *
 * The structural part is copied from the block so the excerpt keeps pointing at
 * the same page/paragraph/cell; only the char range narrows. Out-of-bounds
 * selections return null so a bad range can never be stored as evidence.
 */
export function buildQuoteLocator(
  block: ParsedBlockView,
  start: number,
  length: number,
): EvidenceLocator | null {
  if (!isIndex(start) || !Number.isInteger(length)) return null
  const end = start + length
  if (length <= 0 || end > block.text.length) return null
  const base = block.locator
  switch (base.kind) {
    case "pdf":
      return {
        kind: "pdf", page_number: base.page_number, block_index: base.block_index,
        char_start: start, char_end: end,
      }
    case "docx_paragraph":
      return { kind: "docx_paragraph", paragraph_index: base.paragraph_index, char_start: start, char_end: end }
    case "docx_table":
      return {
        kind: "docx_table", table_index: base.table_index, row_index: base.row_index,
        cell_index: base.cell_index, char_start: start, char_end: end,
      }
  }
}

/** The quoted character range, or null when the locator carries no usable range. */
export function locatorCharRange(
  locator: EvidenceLocator,
): { start: number; end: number } | null {
  const { char_start: start, char_end: end } = locator
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null
  if (start < 0 || end <= start) return null
  return { start, end }
}

/**
 * Structural position only: the page / paragraph / cell, without the char range.
 *
 * Used where the position is a caption rather than an instruction — a report lists
 * a quote and says which page it came from; the character offsets would be noise
 * next to a quote the reader can already see.
 */
export function describeBlock(locator: EvidenceLocator | null | undefined): string {
  if (!locator) return "位置未知"
  switch (locator.kind) {
    case "pdf":
      return `第 ${locator.page_number} 页 · 第 ${locator.block_index} 块`
    case "docx_paragraph":
      return `第 ${locator.paragraph_index} 段`
    case "docx_table":
      return `表格 ${locator.table_index} · 第 ${locator.row_index} 行 · 第 ${locator.cell_index} 列`
  }
}

/** Human-readable position, used as the evidence caption. */
export function describeLocator(locator: EvidenceLocator | null | undefined): string {
  if (!locator) return "位置未知"
  const range = locatorCharRange(locator)
  const suffix = range ? `字符 ${range.start}–${range.end}` : "字符范围未知"
  return `${describeBlock(locator)} · ${suffix}`
}

/**
 * Split a block's text so the quoted sub-range can be rendered as a highlight.
 *
 * Returns null when the range does not fit the block (a stale or cross-document
 * locator): the viewer then shows the whole block unhighlighted instead of
 * asserting a position that is not there.
 */
export function splitByQuotedRange(
  text: string,
  locator: EvidenceLocator,
): { before: string; quoted: string; after: string } | null {
  const range = locatorCharRange(locator)
  if (!range) return null
  if (range.end > text.length) return null
  return {
    before: text.slice(0, range.start),
    quoted: text.slice(range.start, range.end),
    after: text.slice(range.end),
  }
}

/** First block the locator points at, if any. */
export function findBlockIndex(
  blocks: ParsedBlockView[],
  locator: EvidenceLocator | null,
): number | null {
  if (!locator) return null
  const index = blocks.findIndex((block) => locatorMatches(block.locator, locator))
  return index === -1 ? null : index
}

export interface BlockGroup {
  key: string
  label: string
  /** The block plus its position in the payload, so the viewer can key and focus it. */
  blocks: { block: ParsedBlockView; index: number }[]
}

function groupIdentity(locator: EvidenceLocator): { key: string; label: string } {
  switch (locator.kind) {
    case "pdf":
      return { key: `page:${locator.page_number}`, label: `第 ${locator.page_number} 页` }
    case "docx_paragraph":
      // One paragraph per block, so paragraphs share a single run instead of
      // producing one heading per line.
      return { key: "paragraphs", label: "段落" }
    case "docx_table":
      return { key: `table:${locator.table_index}`, label: `表格 ${locator.table_index}` }
  }
}

/**
 * Group parsed blocks the way a reader sees the source file.
 *
 * The viewer renders these groups as headings, so "第 3 页" / "表格 1" must reflect
 * the locator's own coordinates rather than the block's position in the payload.
 */
export function groupBlocks(blocks: ParsedBlockView[]): BlockGroup[] {
  const groups: BlockGroup[] = []
  blocks.forEach((block, index) => {
    const { key, label } = groupIdentity(block.locator)
    const last = groups[groups.length - 1]
    if (last && last.key === key) last.blocks.push({ block, index })
    else groups.push({ key, label, blocks: [{ block, index }] })
  })
  return groups
}

