import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"
import { HardRuleBadge } from "./HardRuleBadge"

describe("HardRuleBadge", () => {
  it("renders the PASS label with the pass class", () => {
    render(<HardRuleBadge outcome="PASS" />)
    const el = screen.getByText("通过")
    expect(el.className).toContain("badge-pass")
  })

  it("renders the FAIL label with the fail class", () => {
    render(<HardRuleBadge outcome="FAIL" />)
    expect(screen.getByText("未通过").className).toContain("badge-fail")
  })

  it("renders the UNKNOWN label with the unknown class", () => {
    render(<HardRuleBadge outcome="UNKNOWN" />)
    expect(screen.getByText("未知").className).toContain("badge-unknown")
  })

  it("renders an unevaluated badge when outcome is null", () => {
    render(<HardRuleBadge outcome={null} />)
    expect(screen.getByText("未评估").className).toContain("badge-muted")
  })
})
