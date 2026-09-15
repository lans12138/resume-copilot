import type { HardRuleOutcome } from "../api/types"

const label: Record<HardRuleOutcome, string> = { PASS: "通过", FAIL: "未通过", UNKNOWN: "未知" }

export function HardRuleBadge({ outcome }: { outcome: HardRuleOutcome | null }) {
  if (outcome === null) return <span className="badge badge-muted">未评估</span>
  return <span className={`badge badge-${outcome.toLowerCase()}`}>{label[outcome]}</span>
}
