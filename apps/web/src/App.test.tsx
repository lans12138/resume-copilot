import { cleanup, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { App, queryClient } from "./App"
import type { Job, ModelModeOut } from "./api/types"
import { useAppStore } from "./state/session"

const currentUser = { id: "11111111-1111-4111-8111-111111111111", username: "hr-demo", role: "HR" as const }
const modelMode: ModelModeOut = {
  mock_model_mode: true,
  source_label: "Mock 模型（确定性假模型，不调用外部服务）",
  chat_model: "mock-chat",
  embedding_model: "mock-embedding",
  embedding_dimension: 1024,
  prompt_version: "v1",
  rule_version: "v1",
}
const job: Job = {
  id: "22222222-2222-4222-8222-222222222222",
  title: "高级后端工程师",
  status: "DRAFT",
  version: 1,
  created_by: currentUser.id,
  created_at: "2026-09-02T00:00:00Z",
  updated_at: "2026-09-02T00:00:00Z",
  current_version: {
    id: "33333333-3333-4333-8333-333333333333",
    version_no: 1,
    description: "建设可靠的招聘服务。",
    requirements: { required_skills: ["python"], preferred_skills: ["docker"], minimum_years_experience: 3, education_level: "本科" },
    content_sha256: "a".repeat(64), schema_version: "v1", created_at: "2026-09-02T00:00:00Z",
  },
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}

beforeEach(() => {
  window.history.replaceState({}, "", "/login")
  window.sessionStorage.clear()
  window.localStorage.clear()
  useAppStore.setState({ accessToken: null, currentUser: null, jobStatusFilter: "ALL", sidebarOpen: false })
  queryClient.clear()
})

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe("authentication and jobs flow", () => {
  it("guards workspace routes and renders the accessible login form", () => {
    window.history.replaceState({}, "", "/jobs")
    render(<App />)
    expect(screen.getByRole("heading", { name: "登录工作台" })).toBeInTheDocument()
    expect(screen.getByLabelText("用户名")).toHaveFocus()
    expect(window.location.pathname).toBe("/login")
  })

  it("logs in, creates a job, and keeps credentials out of localStorage", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith("/auth/token")) return jsonResponse({ access_token: "test-token", token_type: "bearer", expires_in: 1800 })
      if (url.endsWith("/auth/me")) return jsonResponse(currentUser)
      if (url.endsWith("/runtime/model-mode")) return jsonResponse(modelMode)
      if (url.endsWith(`/jobs/${job.id}/assignments`)) return jsonResponse({ items: [], page: 1, page_size: 20, total: 0 })
      if (url.endsWith(`/jobs/${job.id}`)) return jsonResponse(job)
      if (url.endsWith("/jobs") && init?.method === "POST") return jsonResponse(job, 201)
      if (url.endsWith("/match-runs")) return jsonResponse({ runs: [] })
      if (url.endsWith("/applications")) return jsonResponse([])
      if (url.includes("/jobs")) return jsonResponse({ items: [], page: 1, page_size: 20, total: 0 })
      throw new Error(`Unexpected request: ${url}`)
    }))
    const user = userEvent.setup()
    render(<App />)

    await user.type(screen.getByLabelText("用户名"), "hr-demo")
    await user.type(screen.getByLabelText("密码"), "synthetic-password-123")
    await user.click(screen.getByRole("button", { name: "进入工作台" }))
    expect(await screen.findByRole("heading", { name: "岗位工作台" })).toBeInTheDocument()
    // PORT-005: the model mode is stated on every workspace page, so a viewer can tell
    // a scripted answer from a real one before reading anything on screen.
    expect(await screen.findByText(/Mock 模型/)).toBeInTheDocument()
    expect(screen.getByText(/不能证明真实模型效果/)).toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: /新建岗位/ }))
    await user.type(screen.getByLabelText("岗位名称"), job.title)
    await user.type(screen.getByLabelText("岗位说明"), job.current_version.description)
    await user.type(screen.getByLabelText("必备技能"), "Python、PostgreSQL")
    await user.click(screen.getByRole("button", { name: "创建并查看" }))

    expect(await screen.findByRole("heading", { name: job.title })).toBeInTheDocument()
    expect(screen.getByText("草稿")).toBeInTheDocument()
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.getItem("resume-copilot.session")).toContain("test-token")
    await waitFor(() => expect(window.location.pathname).toBe(`/jobs/${job.id}`))
  })
})
