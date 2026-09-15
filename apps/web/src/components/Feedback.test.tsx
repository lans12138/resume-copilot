import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"
import { ApiError } from "../api/client"
import { ErrorNotice } from "./Feedback"

afterEach(cleanup)

describe("ErrorNotice", () => {
  it("distinguishes concurrency conflicts and exposes the request id", () => {
    render(<ErrorNotice error={new ApiError(409, { code: "VERSION_CONFLICT", message: "岗位已被其他请求更新", request_id: "req-42" })} />)
    expect(screen.getByRole("alert")).toHaveTextContent("页面数据已发生变化")
    expect(screen.getByRole("alert")).toHaveTextContent("岗位已被其他请求更新")
    expect(screen.getByRole("alert")).toHaveTextContent("req-42")
  })
})
