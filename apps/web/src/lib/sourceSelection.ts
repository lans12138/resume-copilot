/**
 * Read the character range a reviewer selected inside one parsed block.
 *
 * The selection is expressed in the browser's own tree, but evidence has to be
 * stored as character offsets into the block's plain text (that is what the
 * locator's `char_start`/`char_end` mean to the parser and to every report that
 * cites the chunk). Offsets are therefore measured by measuring the text in
 * front of the selection rather than by trusting `range.startOffset`, which is an
 * offset into a single text node and silently wrong when the block renders as
 * several nodes (mark tags, a highlighted excerpt, …).
 *
 * Returns null when the selection is empty or spans outside the given block: a
 * cross-block selection has no single locator, and pinning it would produce an
 * excerpt that the source viewer cannot highlight.
 */
export function selectionOffsetsWithin(
  element: HTMLElement,
): { start: number; length: number } | null {
  const selection = window.getSelection()
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null
  const range = selection.getRangeAt(0)
  if (!element.contains(range.startContainer) || !element.contains(range.endContainer)) return null

  const prefix = document.createRange()
  prefix.selectNodeContents(element)
  prefix.setEnd(range.startContainer, range.startOffset)
  const start = prefix.toString().length
  const length = range.toString().length
  if (length <= 0) return null
  return { start, length }
}
