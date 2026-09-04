import type { CandidateFilter, CandidateRanking, ChannelName } from "../api/types"

/** Pure, presentation-only filter. Returns a subset; never mutates the input. */
export function applyCandidateFilter(items: CandidateRanking[], filter: CandidateFilter): CandidateRanking[] {
  return items.filter((item) => {
    if (
      filter.hard_rule &&
      filter.hard_rule !== "ALL" &&
      item.hard_rule?.overall !== filter.hard_rule
    ) {
      return false
    }
    if (filter.channel && filter.channel !== "ALL") {
      const channel = filter.channel as ChannelName
      const hit =
        channel === "structured" ? item.structured_rank !== null
        : channel === "keyword" ? item.keyword_rank !== null
        : item.vector_rank !== null
      if (!hit) return false
    }
    return true
  })
}
