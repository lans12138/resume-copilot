import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"
import { ScheduleResult } from "./ScheduleResult"

describe("ScheduleResult", () => {
  it("renders a scheduled interview with its typed status", () => {
    render(
      <ScheduleResult
        proposal={{
          application_id: "12345678-1234-1234-1234-123456789abc",
          duration_minutes: 45,
          timezone: "UTC",
          interviewer_label: "Hiring Manager",
        }}
        status="SCHEDULED"
      />,
    )

    expect(screen.getByText("已排期")).toBeInTheDocument()
    expect(screen.getByText("45 分钟")).toBeInTheDocument()
    expect(screen.getByText("Hiring Manager")).toBeInTheDocument()
  })
})
