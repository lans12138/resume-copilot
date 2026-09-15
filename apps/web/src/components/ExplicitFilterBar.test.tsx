import { afterEach, beforeEach, describe, expect, it } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { ExplicitFilterBar } from "./ExplicitFilterBar"
import { useAppStore } from "../state/session"

beforeEach(() => useAppStore.setState({ candidateHardRule: "ALL", candidateChannel: "ALL" }))
afterEach(() => cleanup())

describe("ExplicitFilterBar", () => {
  it("writes the selected hard rule into the session store", async () => {
    const user = userEvent.setup()
    render(<ExplicitFilterBar />)
    await user.selectOptions(screen.getByLabelText("硬规则"), "FAIL")
    expect(useAppStore.getState().candidateHardRule).toBe("FAIL")
  })

  it("writes the selected channel into the session store", async () => {
    const user = userEvent.setup()
    render(<ExplicitFilterBar />)
    await user.selectOptions(screen.getByLabelText("召回通道"), "vector")
    expect(useAppStore.getState().candidateChannel).toBe("vector")
  })
})
