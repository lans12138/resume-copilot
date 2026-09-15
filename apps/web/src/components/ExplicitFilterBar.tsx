import type { ChannelName, HardRuleOutcome } from "../api/types"
import { useAppStore } from "../state/session"

export function ExplicitFilterBar() {
  const hardRule = useAppStore((state) => state.candidateHardRule)
  const channel = useAppStore((state) => state.candidateChannel)
  const setHardRule = useAppStore((state) => state.setCandidateHardRule)
  const setChannel = useAppStore((state) => state.setCandidateChannel)
  return (
    <div className="toolbar" role="group" aria-label="候选人筛选">
      <label htmlFor="hard-rule-filter">硬规则</label>
      <select
        id="hard-rule-filter"
        value={hardRule}
        onChange={(event) => setHardRule(event.target.value as HardRuleOutcome | "ALL")}
      >
        <option value="ALL">全部</option>
        <option value="PASS">通过</option>
        <option value="FAIL">未通过</option>
        <option value="UNKNOWN">未知</option>
      </select>
      <label htmlFor="channel-filter">召回通道</label>
      <select
        id="channel-filter"
        value={channel}
        onChange={(event) => setChannel(event.target.value as ChannelName | "ALL")}
      >
        <option value="ALL">全部</option>
        <option value="structured">结构化</option>
        <option value="keyword">关键词</option>
        <option value="vector">向量</option>
      </select>
      <span className="muted">显式筛选仅影响展示，不改变匹配范围</span>
    </div>
  )
}
