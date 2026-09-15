/** Document-wide chunk numbering: `(document_id, chunk_index)` is unique server-side. */
export function nextChunkIndex(chunks: readonly { chunk_index: number }[]): number {
  let highest = -1
  for (const chunk of chunks) {
    if (chunk.chunk_index > highest) highest = chunk.chunk_index
  }
  return highest + 1
}
