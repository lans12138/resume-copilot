import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { ApiError } from "../api/client"
import { JobForm } from "./JobForm"

afterEach(cleanup)

describe("JobForm", () => {
  it("links API validation failures to the affected field", () => {
    const error = new ApiError(422, {
      code: "VALIDATION_ERROR",
      message: "请求参数校验失败",
      details: { errors: [{ field: "body.title", type: "string_too_long", message: "too long" }] },
    })
    render(<JobForm submitLabel="保存" busy={false} error={error} onSubmit={vi.fn()} />)
    const title = screen.getByLabelText("岗位名称")
    expect(title).toHaveAttribute("aria-invalid", "true")
    expect(title).toHaveAccessibleDescription("请检查此字段的格式或取值。")
  })
})
