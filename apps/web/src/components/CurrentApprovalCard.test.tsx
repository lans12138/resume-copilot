import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { describe, expect, it } from "vitest"
import type { ApprovalDetail } from "../api/types"
import { CurrentApprovalCard } from "./CurrentApprovalCard"

function approval(overrides: Partial<ApprovalDetail> = {}): ApprovalDetail {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    application_run_id: "22222222-2222-4222-8222-222222222222",
    action_type: "UPDATE_APPLICATION_STATUS",
    status: "PENDING",
    original_params: { target_status: "SHORTLISTED" },
    final_params: null,
    expected_application_version: 3,
    idempotency_key: "run-1:UPDATE_APPLICATION_STATUS",
    version: 2,
    expires_at: null,
    decided_by: null,
    decided_at: null,
    ...overrides,
  }
}

function renderCard(value: ApprovalDetail | null) {
  return render(
    <MemoryRouter>
      <CurrentApprovalCard approval={value} />
    </MemoryRouter>,
  )
}

describe("CurrentApprovalCard", () => {
  // PORT-005: "更新申请状态" alone does not tell a reader whether the candidate is
  // about to be shortlisted or rejected.
  it("says what the pending approval will actually do", () => {
    renderCard(approval())

    expect(screen.getByText("更新申请状态")).toBeInTheDocument()
    expect(screen.getByText("将申请状态更新为「已入围」。")).toBeInTheDocument()
    expect(screen.getByText("审批版本 v2")).toBeInTheDocument()
  })

  it("follows the edited parameters when a human changed them", () => {
    renderCard(approval({ status: "EDITED", final_params: { target_status: "ON_HOLD" } }))

    expect(screen.getByText("将申请状态更新为「暂缓」。")).toBeInTheDocument()
  })

  it("says whether the write has happened yet", () => {
    renderCard(approval({ status: "APPROVED" }))

    expect(screen.getByText(/尚未写入/)).toBeInTheDocument()
  })

  it("says so when there is nothing to decide", () => {
    renderCard(null)

    expect(screen.getByText("该流程当前没有等待决策的审批")).toBeInTheDocument()
  })
})
