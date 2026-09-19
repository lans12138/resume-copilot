import { describe, expect, it } from "vitest"
import type { AgentEventEnvelope } from "../api/types"
import { describeEvent, messageLabel, nodeLabel, statusLabel, statusTone } from "./runTimeline"

function event(overrides: Partial<AgentEventEnvelope> = {}): AgentEventEnvelope {
  return {
    event_id: "e-1",
    run_id: "r-1",
    run_type: "APPLICATION",
    sequence: 1,
    event_type: "NODE_STARTED",
    node: null,
    status: "RUNNING",
    message_key: "",
    safe_payload: {},
    occurred_at: null,
    ...overrides,
  }
}

describe("statusLabel", () => {
  it("names each run status in Chinese", () => {
    expect(statusLabel("WAITING_APPROVAL")).toBe("等待审批")
    expect(statusLabel("COMPLETED")).toBe("已完成")
  })

  it("shows an unknown status rather than hiding it", () => {
    // A status added by a newer backend must appear, not vanish behind "未知".
    expect(statusLabel("PAUSED")).toBe("PAUSED")
    expect(statusTone("PAUSED")).toBe("muted")
  })
})

describe("nodeLabel", () => {
  it("names the ApplicationRun and MatchRun nodes", () => {
    expect(nodeLabel("human_review")).toBe("等待人工审批")
    expect(nodeLabel("score_with_evidence")).toBe("证据化评分")
  })

  it("passes an unknown node through", () => {
    expect(nodeLabel("new_node")).toBe("new_node")
  })
})

describe("messageLabel", () => {
  it("explains the message keys the backend emits", () => {
    expect(messageLabel("run.interrupted")).toContain("暂停")
  })

  it("passes an unknown key through", () => {
    expect(messageLabel("node.new_node.started")).toBe("node.new_node.started")
  })
})

describe("describeEvent", () => {
  it("names a node event by the node and its phase", () => {
    const row = describeEvent(
      event({ event_type: "NODE_STARTED", node: "retrieve_candidates" }),
    )

    expect(row.title).toBe("召回候选人 · 开始")
  })

  it("names a terminal event by what happened to the run, not by the node", () => {
    // Naming it after the node would send an operator looking for a local problem
    // where the run actually ended.
    const row = describeEvent(
      event({
        event_type: "RUN_FAILED",
        node: "human_review",
        status: "FAILED",
        message_key: "run.failed",
        safe_payload: { reason: "APPROVAL_EXPIRED", error_code: "APPROVAL_EXPIRED", retryable: true },
      }),
    )

    expect(row.title).toBe("流程失败")
    expect(row.tone).toBe("danger")
  })

  it("names a rejection as a rejection, not as a completed run", () => {
    // A rejected approval and a completed run both emit RUN_COMPLETED, so the event
    // type alone cannot title the row.
    const row = describeEvent(
      event({ event_type: "RUN_COMPLETED", status: "COMPLETED", message_key: "run.rejected" }),
    )

    expect(row.title).toBe("审批驳回，流程结束")
    expect(row.detail).toBe("人工审批驳回，流程结束且没有写入副作用")
  })

  it("keeps the message as context only when it adds something", () => {
    const paused = describeEvent(
      event({
        event_type: "STATUS_CHANGED",
        node: "human_review",
        status: "WAITING_APPROVAL",
        message_key: "run.interrupted",
      }),
    )
    expect(paused.title).toBe("等待人工审批")
    expect(paused.detail).toBe("流程在人工审批处暂停，等待决策")

    const duplicate = describeEvent(
      event({ event_type: "RUN_COMPLETED", status: "COMPLETED", message_key: "run.completed" }),
    )
    expect(duplicate.title).toBe("流程完成")
    expect(duplicate.detail).toBeNull()
  })

  it("translates the status it shows beside the title", () => {
    const row = describeEvent(event({ status: "WAITING_APPROVAL" }))

    expect(row.status).toBe("等待审批")
    expect(row.tone).toBe("info")
  })

  it("falls back to the message when there is no node to name", () => {
    const row = describeEvent(event({ event_type: "STATUS_CHANGED", message_key: "match_run.created" }))

    expect(row.title).toBe("批量分析已创建")
  })

  it("keeps every identifier for the collapsed detail panel", () => {
    const row = describeEvent(
      event({
        event_type: "NODE_COMPLETED",
        node: "hard_rule_evaluate",
        message_key: "node.hard_rule_evaluate.completed",
        safe_payload: { candidate_profile_id: "abc" },
      }),
    )

    expect(row.technical.event_type).toBe("NODE_COMPLETED")
    expect(row.technical.node).toBe("hard_rule_evaluate")
    expect(row.technical.message_key).toBe("node.hard_rule_evaluate.completed")
    expect(row.technical.payload).toContain("abc")
  })

  it("carries no payload string when the event has none", () => {
    expect(describeEvent(event()).technical.payload).toBeNull()
  })
})

describe("describeEvent failures", () => {
  it("explains a known failure code in words and keeps the code", () => {
    const row = describeEvent(
      event({
        event_type: "RUN_FAILED",
        status: "FAILED",
        message_key: "run.failed",
        safe_payload: { reason: "schedule_backend_unavailable", error_code: "SCHEDULE_BACKEND_UNAVAILABLE" },
      }),
    )

    expect(row.failure?.summary).toBe("排期后端不可用，面试安排未创建")
    expect(row.failure?.code).toBe("SCHEDULE_BACKEND_UNAVAILABLE")
  })

  it("admits it does not know an unrecognised code instead of inventing one", () => {
    const row = describeEvent(
      event({ event_type: "RUN_FAILED", status: "FAILED", safe_payload: { error_code: "SOMETHING_NEW" } }),
    )

    expect(row.failure?.summary).toBe("流程失败")
    expect(row.failure?.code).toBe("SOMETHING_NEW")
  })

  it("reports a per-candidate failure while the run itself continues", () => {
    // score_with_evidence isolates one candidate's fault: the run still completes,
    // so a failure row must not be read as "the run failed".
    const row = describeEvent(
      event({
        event_type: "NODE_COMPLETED",
        node: "score_with_evidence",
        status: "FAILED",
        message_key: "node.score_with_evidence.failed",
        safe_payload: {
          candidate_profile_id: "11111111-1111-4111-8111-111111111111",
          error_code: "CANDIDATE_SCORING_FAILED",
        },
      }),
    )

    expect(row.title).toBe("证据化评分 · 完成")
    expect(row.failure?.summary).toBe("该候选人评分失败，其余候选人不受影响")
    expect(row.failure?.candidate).toBe("11111111")
  })

  it("shows no failure on an ordinary event", () => {
    expect(describeEvent(event({ node: "finalize_run" })).failure).toBeNull()
  })

  it("does not treat a successful run's retryable flag as a failure", () => {
    const row = describeEvent(
      event({ event_type: "RUN_COMPLETED", status: "COMPLETED", safe_payload: { retryable: true } }),
    )

    expect(row.failure).toBeNull()
  })

  it("says why a cancelled run was cancelled without calling it a failure", () => {
    // Cancellation is not a failure, so it gets no failure row — but the reason is
    // the only place the *why* is recorded.
    const row = describeEvent(
      event({
        event_type: "RUN_CANCELLED",
        status: "CANCELLED",
        message_key: "run.cancelled",
        safe_payload: { reason: "user_cancel" },
      }),
    )

    expect(row.title).toBe("流程已取消")
    expect(row.failure).toBeNull()
    expect(row.detail).toBe("由用户取消")
  })

  it("keeps an unrecognised reason out of the reading line", () => {
    // "some_new_token" is a code, not an explanation; it stays in the payload.
    const row = describeEvent(
      event({
        event_type: "RUN_CANCELLED",
        status: "CANCELLED",
        message_key: "run.cancelled",
        safe_payload: { reason: "some_new_token" },
      }),
    )

    expect(row.detail).toBeNull()
    expect(row.technical.payload).toContain("some_new_token")
  })

  it("never speaks an unmapped message key", () => {
    // A `node.*` key has no mapping and must not become the explanation — printing
    // `node.score_with_evidence.completed` is the problem this module removes.
    const row = describeEvent(
      event({
        event_type: "NODE_COMPLETED",
        node: "score_with_evidence",
        message_key: "node.score_with_evidence.completed",
      }),
    )

    expect(row.title).toBe("证据化评分 · 完成")
    expect(row.detail).toBeNull()
    expect(row.technical.message_key).toBe("node.score_with_evidence.completed")
  })
})
