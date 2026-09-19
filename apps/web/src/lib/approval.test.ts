import { describe, expect, it } from "vitest"
import {
  actionLabel,
  awaitingExecution,
  describeAction,
  describeOutcome,
  diffParams,
  isDecidable,
  paramLabel,
  paramValue,
} from "./approval"

describe("paramLabel / paramValue", () => {
  it("names the payload keys the backend proposes", () => {
    expect(paramLabel("target_status")).toBe("目标状态")
    expect(paramLabel("interviewer_label")).toBe("面试官")
  })

  it("passes an unknown key through rather than hiding the parameter", () => {
    // A payload field added later must stay visible: a reviewer approving a write
    // they cannot read is worse than one reading a raw key.
    expect(paramLabel("new_field")).toBe("new_field")
  })

  it("translates a status and keeps the stored form beside it", () => {
    expect(paramValue("target_status", "SHORTLISTED")).toBe("已入围（SHORTLISTED）")
  })

  it("shows an unrecognised status value as itself", () => {
    expect(paramValue("target_status", "ARCHIVED")).toBe("ARCHIVED")
  })

  it("renders non-string values", () => {
    expect(paramValue("duration_minutes", 45)).toBe("45")
    expect(paramValue("timezone", null)).toBe("—")
  })
})

describe("describeAction", () => {
  it("names the status a proposal will apply", () => {
    expect(describeAction("UPDATE_APPLICATION_STATUS", { target_status: "SHORTLISTED" })).toBe(
      "将申请状态更新为「已入围」。",
    )
  })

  it("spells out the schedule the proposal will create", () => {
    expect(
      describeAction("CREATE_INTERVIEW_SCHEDULE", {
        duration_minutes: 45,
        timezone: "UTC",
        interviewer_label: "Hiring Manager",
      }),
    ).toBe("创建面试安排：45 分钟 · 时区 UTC · 面试官 Hiring Manager。")
  })

  it("still says something useful when the parameters are missing", () => {
    expect(describeAction("UPDATE_APPLICATION_STATUS", null)).toContain("目标状态")
    expect(describeAction("CREATE_INTERVIEW_SCHEDULE", {})).toBe("创建一条面试安排。")
  })
})

describe("diffParams", () => {
  it("reports an untouched proposal as unchanged", () => {
    const changes = diffParams({ target_status: "SHORTLISTED" }, { target_status: "SHORTLISTED" })

    expect(changes).toHaveLength(1)
    expect(changes[0].kind).toBe("same")
    expect(changes[0].label).toBe("目标状态")
  })

  it("reports an edit as changed and shows both sides", () => {
    const changes = diffParams({ target_status: "SHORTLISTED" }, { target_status: "ON_HOLD" })

    expect(changes[0].kind).toBe("changed")
    expect(changes[0].before).toBe("已入围（SHORTLISTED）")
    expect(changes[0].after).toBe("暂缓（ON_HOLD）")
  })

  it("reports a removed parameter as a change, not as silence", () => {
    // Two independent dumps made a deleted key vanish from the right-hand column,
    // which reads as "unchanged" rather than as a change.
    const changes = diffParams(
      { target_status: "SHORTLISTED", interviewer_label: "Hiring Manager" },
      { target_status: "SHORTLISTED" },
    )

    const removed = changes.find((change) => change.key === "interviewer_label")
    expect(removed?.kind).toBe("removed")
    expect(removed?.after).toBeNull()
  })

  it("reports a parameter the edit introduced", () => {
    const changes = diffParams({ target_status: "SHORTLISTED" }, { target_status: "SHORTLISTED", note: "urgent" })

    expect(changes.find((change) => change.key === "note")?.kind).toBe("added")
  })

  it("keeps the original proposal's ordering", () => {
    const changes = diffParams(
      { duration_minutes: 45, timezone: "UTC" },
      { timezone: "Asia/Shanghai", duration_minutes: 45 },
    )

    expect(changes.map((change) => change.key)).toEqual(["duration_minutes", "timezone"])
  })

  it("treats two empty sides as no parameters", () => {
    expect(diffParams(null, null)).toEqual([])
  })
})

describe("describeOutcome", () => {
  it("separates a recorded decision from an applied one", () => {
    // The distinction the page previously hid: APPROVED means the write has not
    // happened yet, EXECUTED means it landed exactly once.
    expect(describeOutcome("APPROVED").label).toContain("等待执行")
    expect(describeOutcome("APPROVED").detail).toContain("尚未写入")
    expect(describeOutcome("EXECUTED").label).toBe("已执行")
    expect(describeOutcome("EXECUTED").detail).toContain("只应用了一次")
  })

  it("says a rejection wrote nothing", () => {
    expect(describeOutcome("REJECTED").detail).toBe("没有执行任何写入。")
  })

  it("gives a recovery path for a failed execution and for an expiry", () => {
    expect(describeOutcome("EXECUTION_FAILED").guidance).toContain("重试")
    expect(describeOutcome("EXPIRED").guidance).toContain("重新发起")
  })

  it("offers no next step where there is none", () => {
    expect(describeOutcome("PENDING").guidance).toBeNull()
    expect(describeOutcome("EXECUTED").guidance).toBeNull()
    expect(describeOutcome("REJECTED").guidance).toBeNull()
  })
})

describe("awaitingExecution / isDecidable", () => {
  it("polls only while the side effect has not been applied", () => {
    expect(awaitingExecution("APPROVED")).toBe(true)
    expect(awaitingExecution("EDITED")).toBe(true)
    expect(awaitingExecution("EXECUTED")).toBe(false)
    expect(awaitingExecution("EXECUTION_FAILED")).toBe(false)
    expect(awaitingExecution("PENDING")).toBe(false)
  })

  it("lets only a pending approval be decided", () => {
    expect(isDecidable("PENDING")).toBe(true)
    for (const status of ["APPROVED", "EDITED", "EXECUTED", "EXECUTION_FAILED", "REJECTED", "EXPIRED"] as const) {
      expect(isDecidable(status)).toBe(false)
    }
  })
})

describe("actionLabel", () => {
  it("names both actions", () => {
    expect(actionLabel("UPDATE_APPLICATION_STATUS")).toBe("更新申请状态")
    expect(actionLabel("CREATE_INTERVIEW_SCHEDULE")).toBe("创建面试安排")
  })
})
