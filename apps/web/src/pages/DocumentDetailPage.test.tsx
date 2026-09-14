import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { DocumentDetailPage } from "./DocumentDetailPage"
import { useAppStore } from "../state/session"
import type { DocumentContent, DocumentSummary } from "../api/types"

const DOCUMENT_ID = "11111111-1111-4111-8111-111111111111"

function summary(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id: DOCUMENT_ID,
    original_filename: "candidate.pdf",
    media_type: "application/pdf",
    size_bytes: 48213,
    content_sha256: "a".repeat(64),
    status: "FAILED",
    parser_version: "parser-v1",
    attempt: 2,
    retryable: true,
    error_code: "STORAGE_UNAVAILABLE",
    error_message: "文件读取失败，请稍后重试",
    uploaded_by: "44444444-4444-4444-8444-444444444444",
    created_at: "2026-09-14T02:00:00Z",
    updated_at: "2026-09-14T02:30:00Z",
    ...overrides,
  }
}

const content: DocumentContent = {
  document_id: DOCUMENT_ID,
  media_type: "application/pdf",
  full_text: "张伟 6 年 Python 后端开发经验",
  page_count: 2,
  paragraph_count: null,
  table_count: null,
  warnings: ["第 2 页包含无法识别的水印文本"],
  blocks: [
    { block_index: 0, text: "张伟", locator: { kind: "pdf", page_number: 1, block_index: 0, char_start: 0, char_end: 2 } },
  ],
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}

function stubApi(options: { document: DocumentSummary; contentUnavailable?: boolean }) {
  const calls: string[] = []
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    calls.push(url)
    if (init?.method === "POST") return jsonResponse({ ...options.document, status: "QUEUED", attempt: 3 })
    if (url.includes("/content")) {
      if (options.contentUnavailable) {
        return jsonResponse({ code: "DOCUMENT_CONTENT_UNAVAILABLE", message: "解析内容不可用" }, 422)
      }
      return jsonResponse(content)
    }
    return jsonResponse(options.document)
  }))
  return { calls }
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/documents/${DOCUMENT_ID}`]}>
        <Routes>
          <Route path="/documents/:documentId" element={<DocumentDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  useAppStore.setState({ accessToken: "test-token", currentUser: null })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  useAppStore.setState({ accessToken: null })
})

describe("DocumentDetailPage", () => {
  it("separates the failure category, the backend message and the error code", async () => {
    stubApi({ document: summary() })
    renderPage()

    expect(await screen.findByText("存储暂时不可用")).toBeInTheDocument()
    expect(screen.getByText("文件读取失败，请稍后重试")).toBeInTheDocument()
    expect(screen.getByText("错误代码：STORAGE_UNAVAILABLE")).toBeInTheDocument()
  })

  it("shows parse counts and warnings when the content is readable", async () => {
    stubApi({ document: { ...summary(), status: "REVIEW_REQUIRED", error_code: null, error_message: null } })
    renderPage()

    expect(await screen.findByText("第 2 页包含无法识别的水印文本")).toBeInTheDocument()
    expect(screen.getByText("可定位块")).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "去校对资料 →" })).toHaveAttribute(
      "href",
      `/documents/${DOCUMENT_ID}/review`,
    )
  })

  it("retries a failed parse only when the backend flagged it retryable", async () => {
    const { calls } = stubApi({ document: summary() })
    renderPage()

    await userEvent.click(await screen.findByRole("button", { name: "重试解析" }))
    await waitFor(() => expect(calls.some((url) => url.endsWith(`/documents/${DOCUMENT_ID}/retry`))).toBe(true))
  })

  it("does not offer a retry the backend would reject", async () => {
    stubApi({ document: summary({ error_code: "EMPTY_TEXT", retryable: false }) })
    renderPage()

    expect(await screen.findByText("未检测到可提取文本（可能是扫描件）")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "重试解析" })).toBeNull()
  })

  it("explains that the source text is unavailable without hiding the rest", async () => {
    stubApi({ document: { ...summary(), status: "REVIEW_REQUIRED", error_code: null, error_message: null }, contentUnavailable: true })
    renderPage()

    expect(await screen.findByText("原文暂不可用")).toBeInTheDocument()
    expect(screen.getByText("解析结果")).toBeInTheDocument()
  })
})
