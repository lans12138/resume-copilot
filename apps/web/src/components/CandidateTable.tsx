import { Link } from "react-router-dom"
import type { CandidateRanking } from "../api/types"
import { HardRuleBadge } from "./HardRuleBadge"

function channelCell(rank: number | null, score: number | null) {
  if (rank === null || score === null) return <span className="muted">—</span>
  return <span>#{rank} · {score.toFixed(3)}</span>
}

export function CandidateTable({ items, jobId }: { items: CandidateRanking[]; jobId?: string }) {
  return (
    <table className="ranking-table">
      <thead>
        <tr>
          <th scope="col">排名</th>
          <th scope="col">候选人</th>
          <th scope="col">RRF</th>
          <th scope="col">硬规则</th>
          <th scope="col">结构化</th>
          <th scope="col">关键词</th>
          <th scope="col">向量</th>
        </tr>
      </thead>
      <tbody>
        {items.map((candidate) => (
          <tr key={candidate.candidate_profile_id}>
            <td>{candidate.snapshot_order}</td>
            <td>
              {/* The job travels in the URL: the profile and its evidence are read
                  under job-level authorization, so the link must not be ambiguous. */}
              {jobId ? (
                <Link to={`/candidates/${candidate.candidate_profile_id}?job=${jobId}`}>
                  <strong>{candidate.display_name || "候选人"}</strong>
                </Link>
              ) : (
                <strong>{candidate.display_name || "候选人"}</strong>
              )}
              <small>{candidate.normalized_skills.slice(0, 4).join("、")}</small>
            </td>
            <td>{candidate.rrf_score.toFixed(4)}</td>
            <td><HardRuleBadge outcome={candidate.hard_rule?.overall ?? null} /></td>
            <td>{channelCell(candidate.structured_rank, candidate.structured_score)}</td>
            <td>{channelCell(candidate.keyword_rank, candidate.keyword_score)}</td>
            <td>{channelCell(candidate.vector_rank, candidate.vector_score)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
