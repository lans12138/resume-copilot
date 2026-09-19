import { cleanup, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { ModelModeOut } from "../api/types"
import { useAppStore } from "../state/session"
import { ModelModeBanner } from "./ModelModeBanner"

const MOCK_MODE: ModelModeOut = {
  mock_model_mode: true,
  source_label: "Mock 模型（确定性假模型，不调用外部服务）",
  chat_model: "mock-chat",
  embedding_model: "mock-embedding",
  embedding_dimension: 1024,
  prompt_version: "v1",
  rule_version: "v1",
}

const LIVE_MODE: ModelModeOut = {
  mock_model_mode: false,
  source_label: "真实模型",
  chat_model: "qwen-plus",
  embedding_model: "text-embedding-v4",
  embedding_dimension: 1024,
  prompt_version: "v1",
  rule_version: "v1",
}

function stubMode(body: unknown, status = 200) {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  )
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

function renderBanner() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <ModelModeBanner />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  useAppStore.setState({ accessToken: "test-token", currentUser: null })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("ModelModeBanner", () => {
  // PORT-005: a recorded evaluation only proves what it proves if the reader knows
  // which adapter produced it, so the fake must be named rather than implied.
  it("names the fake model and says what it cannot prove", async () => {
    stubMode(MOCK_MODE)
    renderBanner()

    expect(await screen.findByText(/Mock 模型/)).toBeInTheDocument()
    expect(screen.getByText(/不能证明真实模型效果/)).toBeInTheDocument()
  })

  it("states the configured models behind the mode", async () => {
    stubMode(MOCK_MODE)
    renderBanner()

    expect(await screen.findByText("mock-chat")).toBeInTheDocument()
    expect(screen.getByText("mock-embedding")).toBeInTheDocument()
    expect(screen.getByText(/1024 维/)).toBeInTheDocument()
    expect(screen.getByText("对话模型")).toBeInTheDocument()
  })

  it("does not claim the fake when the deployment is configured for a real model", async () => {
    stubMode(LIVE_MODE)
    renderBanner()

    expect(await screen.findByText("真实模型")).toBeInTheDocument()
    expect(screen.getByText(/由配置的外部模型生成/)).toBeInTheDocument()
    expect(screen.queryByText(/不能证明真实模型效果/)).not.toBeInTheDocument()
  })

  // "we could not confirm the mode" is a different statement from "it is not mock";
  // rendering nothing would let a viewer assume the safer of the two.
  it("admits it when the mode cannot be read", async () => {
    stubMode({ detail: "boom" }, 500)
    renderBanner()

    expect(await screen.findByText("模型模式未知")).toBeInTheDocument()
    expect(screen.getByText(/请勿据本页结论推断模型效果/)).toBeInTheDocument()
  })

  it("says nothing and asks nothing before there is a session", async () => {
    useAppStore.setState({ accessToken: null })
    const fetchMock = stubMode(MOCK_MODE)
    const { container } = renderBanner()

    await waitFor(() => expect(container).toBeEmptyDOMElement())
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
