import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { ProfileForm, type ProfileFormSubmission } from "./ProfileForm"
import type { CandidateProfile } from "../api/types"

afterEach(() => cleanup())

function profile(overrides: Partial<CandidateProfile> = {}): CandidateProfile {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    candidate_id: "22222222-2222-4222-8222-222222222222",
    document_id: "33333333-3333-4333-8333-333333333333",
    version_no: 1,
    status: "REVIEW_REQUIRED",
    profile_json: {
      full_name: "张伟",
      summary: "6 年后端开发经验",
      skills: [{ name: "Python", years: 6 }],
      education_level: "BACHELOR",
    },
    normalized_skills: ["Python", "FastAPI"],
    years_experience: 6,
    education_level: "BACHELOR",
    schema_version: "v1",
    confirmed_by: null,
    confirmed_at: null,
    created_at: "2026-09-14T02:00:00Z",
    updated_at: "2026-09-14T02:00:00Z",
    version: 3,
    ...overrides,
  }
}

describe("ProfileForm", () => {
  it("starts from the extracted draft and submits the edited values", async () => {
    const onSubmit = vi.fn<(submission: ProfileFormSubmission) => void>()
    render(<ProfileForm profile={profile()} onSubmit={onSubmit} />)

    expect(screen.getByLabelText("姓名")).toHaveValue("张伟")
    expect(screen.getByLabelText("工作年限")).toHaveValue("6")
    expect(screen.getByLabelText("学历")).toHaveValue("BACHELOR")

    await userEvent.clear(screen.getByLabelText("姓名"))
    await userEvent.type(screen.getByLabelText("姓名"), "张伟明")
    await userEvent.selectOptions(screen.getByLabelText("学历"), "MASTER")
    await userEvent.click(screen.getByRole("button", { name: "确认资料" }))

    expect(onSubmit).toHaveBeenCalledTimes(1)
    const { edit } = onSubmit.mock.calls[0][0]
    expect(edit.years_experience).toBe(6)
    expect(edit.normalized_skills).toEqual(["Python", "FastAPI"])
    // The column and the JSON view of education must not disagree: reports cite the
    // JSON, filtering reads the column.
    expect(edit.education_level).toBe("MASTER")
    expect(edit.profile_json["education_level"]).toBe("MASTER")
    expect(edit.profile_json["full_name"]).toBe("张伟明")
  })

  it("refuses a value the backend would reject anyway", async () => {
    const onSubmit = vi.fn()
    render(<ProfileForm profile={profile()} onSubmit={onSubmit} />)

    await userEvent.clear(screen.getByLabelText("工作年限"))
    await userEvent.type(screen.getByLabelText("工作年限"), "-3")
    await userEvent.click(screen.getByRole("button", { name: "确认资料" }))

    expect(screen.getByRole("alert")).toHaveTextContent("工作年限需为 0 到 60 之间的数字")
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it("normalizes the free-text skill list before submitting", async () => {
    const onSubmit = vi.fn<(submission: ProfileFormSubmission) => void>()
    render(<ProfileForm profile={profile()} onSubmit={onSubmit} />)

    await userEvent.clear(screen.getByLabelText("技能（用、或逗号分隔）"))
    await userEvent.type(screen.getByLabelText("技能（用、或逗号分隔）"), "Go、gRPC，go")
    await userEvent.click(screen.getByRole("button", { name: "确认资料" }))

    expect(onSubmit.mock.calls[0][0].edit.normalized_skills).toEqual(["Go", "gRPC"])
  })

  it("explains why a confirmed profile can no longer be edited", () => {
    render(
      <ProfileForm
        profile={profile({ status: "READY", confirmed_at: "2026-09-14T03:00:00Z" })}
        onSubmit={vi.fn()}
        disabled
        disabledReason="该资料当前为「已就绪」，只有待校对的草稿可以确认。"
      />,
    )

    expect(screen.getByLabelText("姓名")).toBeDisabled()
    expect(screen.getByRole("button", { name: "确认资料" })).toBeDisabled()
    expect(screen.getByText("该资料当前为「已就绪」，只有待校对的草稿可以确认。")).toBeInTheDocument()
    expect(screen.getByText(/已于/)).toBeInTheDocument()
  })

  it("still explains itself when the caller gives no reason", () => {
    render(<ProfileForm profile={profile()} onSubmit={vi.fn()} disabled />)
    expect(screen.getByText("当前资料不在待校对状态，无法再次确认。")).toBeInTheDocument()
  })

  it("restores the extracted values when the reviewer resets", async () => {
    render(<ProfileForm profile={profile()} onSubmit={vi.fn()} />)

    await userEvent.clear(screen.getByLabelText("姓名"))
    await userEvent.type(screen.getByLabelText("姓名"), "写错了")
    await userEvent.click(screen.getByRole("button", { name: "重置为当前资料" }))

    expect(screen.getByLabelText("姓名")).toHaveValue("张伟")
  })
})
