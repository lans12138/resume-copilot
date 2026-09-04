import type { MatchRunCandidate } from "../api/types"
import { HardRuleBadge } from "./HardRuleBadge"

const processingLabel: Record<MatchRunCandidate["processing_status"], string> = {
  PENDING: "待处理",
  COMPLETED: "已完成",
  FAILED: "失败",
}

/** Ranking of recalled candidates for a MatchRun (detailed design §15.4). */
export function RankingTable({ candidates }: { candidates: MatchRunCandidate[] }) {
  if (candidates.length === 0) {
    return <p className="muted">本次分析没有命中候选人（Top-K 为空）。</p>
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>排序</th>
            <th>候选人</th>
            <th>RRF 分数</th>
            <th>处理状态</th>
            <th>硬规则</th>
          </tr>
        </thead>
        <tbody>
          {candidates.map((c) => (
            <tr key={c.candidate_profile_id}>
              <td>{c.snapshot_order}</td>
              <td><code>{c.candidate_profile_id.slice(0, 8)}</code></td>
              <td>{c.rrf_score.toFixed(4)}</td>
              <td>{processingLabel[c.processing_status]}</td>
              <td>
                {c.hard_rule_overall === null ? (
                  <span className="badge badge-muted">未评估</span>
                ) : (
                  <HardRuleBadge outcome={c.hard_rule_overall as "PASS" | "FAIL" | "UNKNOWN"} />
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
