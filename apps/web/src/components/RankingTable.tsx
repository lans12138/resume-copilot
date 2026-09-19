import { Link } from "react-router-dom"
import type { MatchRunCandidate } from "../api/types"
import { candidateFacts, candidateName, shortProfileId } from "../lib/candidateDisplay"
import { HardRuleBadge } from "./HardRuleBadge"

const processingLabel: Record<MatchRunCandidate["processing_status"], string> = {
  PENDING: "待处理",
  COMPLETED: "已完成",
  FAILED: "失败",
}

/**
 * Ranking of recalled candidates for a MatchRun (detailed design §15.4).
 *
 * PORT-005: the 候选人 column leads with the name and a short summary, because the
 * ranking table is the page a demo starts from — the presenter connects a row to
 * its report and its approval out loud, and a truncated UUID cannot be said. The
 * profile id stays visible as an explicitly labelled tracking detail: it is what
 * support and the API use, so it must not be removed, only demoted.
 */
export function RankingTable({
  candidates,
  jobId,
  onStartApplication,
  startingApplicationId,
}: {
  candidates: MatchRunCandidate[]
  /** Enables the per-candidate link; the profile is read under job authorization. */
  jobId?: string
  onStartApplication?: (applicationId: string) => void
  startingApplicationId?: string
}) {
  if (candidates.length === 0) {
    return (
      <div className="empty-state">
        <strong>本次分析没有命中候选人</strong>
        <span>
          没有候选人进入 Top-K。可以检查岗位必备技能与学历要求是否过严，或确认候选人资料是否已确认（只有已确认的资料参与召回）。
        </span>
      </div>
    )
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
            {onStartApplication ? <th>操作</th> : null}
          </tr>
        </thead>
        <tbody>
          {candidates.map((c) => {
            const facts = candidateFacts(c)
            return (
              <tr key={c.candidate_profile_id}>
                <td>{c.snapshot_order}</td>
                <td>
                  {jobId ? (
                    // The job travels in the URL: a profile and its evidence are read
                    // under job-level authorization, so the link must not be ambiguous.
                    <Link to={`/candidates/${c.candidate_profile_id}?job=${jobId}`}>
                      <strong>{candidateName(c.display_name)}</strong>
                    </Link>
                  ) : (
                    <strong>{candidateName(c.display_name)}</strong>
                  )}
                  {facts.length > 0 ? <small>{facts.join(" · ")}</small> : null}
                  <small>
                    辅助追踪 <code>{shortProfileId(c.candidate_profile_id)}</code>
                  </small>
                </td>
                <td>{c.rrf_score.toFixed(4)}</td>
                <td>{processingLabel[c.processing_status]}</td>
                <td>
                  {c.hard_rule_overall === null ? (
                    <span className="badge badge-muted">未评估</span>
                  ) : (
                    <HardRuleBadge outcome={c.hard_rule_overall as "PASS" | "FAIL" | "UNKNOWN"} />
                  )}
                </td>
                {onStartApplication ? (
                  <td>
                    <button
                      className="button button-ghost button-small"
                      type="button"
                      disabled={
                        c.processing_status !== "COMPLETED" ||
                        startingApplicationId !== undefined
                      }
                      onClick={() => onStartApplication(c.application_id)}
                    >
                      {startingApplicationId === c.application_id ? "启动中…" : "启动单人流程"}
                    </button>
                  </td>
                ) : null}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
