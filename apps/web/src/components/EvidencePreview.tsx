import type { EvidenceChunk, EvidenceLocator } from "../api/types"
import { describeLocator, locatorMatches, parseLocator } from "../lib/locator"

/**
 * Evidence pinned to a profile (FIN-004).
 *
 * Every row states where the excerpt came from. A chunk whose locator cannot be
 * narrowed still renders — with "位置未知" and no locate action — because hiding
 * it would be worse: the reviewer has to know the claim is backed by *something*,
 * and that the position could not be resolved.
 */
export function EvidencePreview({
  chunks,
  activeLocator,
  onLocate,
}: {
  chunks: EvidenceChunk[]
  activeLocator: EvidenceLocator | null
  onLocate?: (locator: EvidenceLocator) => void
}) {
  if (chunks.length === 0) {
    return (
      <div className="empty-state">
        <strong>还没有钉住的证据</strong>
        <span>在左侧原文中选中片段并钉为证据，结论才能引用到具体出处。</span>
      </div>
    )
  }

  return (
    <ol className="evidence-list">
      {chunks.map((chunk) => {
        const locator = parseLocator(chunk.locator_json)
        const isActive = locator !== null && activeLocator !== null && locatorMatches(locator, activeLocator)
        return (
          <li key={chunk.id} className={`evidence-item${isActive ? " is-active" : ""}`}>
            <div className="evidence-head">
              <span className="badge badge-neutral">{chunk.section_type}</span>
              <small>{describeLocator(locator)}</small>
              {locator && onLocate ? (
                <button
                  type="button"
                  className="button button-ghost button-small"
                  onClick={() => onLocate(locator)}
                >
                  定位原文
                </button>
              ) : null}
            </div>
            <p className="evidence">{chunk.text}</p>
          </li>
        )
      })}
    </ol>
  )
}
