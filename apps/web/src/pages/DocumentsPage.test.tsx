import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter } from "react-router-dom"
import { DocumentsPage } from "./DocumentsPage"
import { useAppStore } from "../state/session"
import type { DocumentSummary } from "../api/types"

const queueItem: DocumentSummary = {
  id: "11111111-1111-4111-8111-111111111111",
  original_filename: "candidate.docx",
  media_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  size_bytes: 4096,
  content_sha256: "b".repeat(64),
  status: "FAILED",
  parser_version: "parser-v1",
  attempt: 1,
  retryable: true,
  error_code: "STORAGE_UNAVAILABLE",
  error_message: "文件读取失败，请稍后重试",
  uploaded_by: "22222222-2222-4222-8222-222222222222",
  created_at: "2026-09-14T02:00:00Z",
  updated_at: "2026-09-14T02:00:00Z",
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  // The queue links each row to the document detail route.
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DocumentsPage />
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

describe("DocumentsPage", () => {
  it("lists the queue and sends the status filter to the server", async () => {
    const calls: string[] = []
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      calls.push(String(input))
      return jsonResponse({ items: [queueItem], page: 1, page_size: 50, total: 1 })
    }))

    renderPage()

    expect(await screen.findByText("candidate.docx")).toBeInTheDocument()
    expect(screen.getByText("存储暂时不可用")).toBeInTheDocument()
    expect(screen.getByText("文件读取失败，请稍后重试")).toBeInTheDocument()

    await userEvent.selectOptions(screen.getByLabelText("状态筛选"), "FAILED")
    // The count is a server fact, so the filter must travel with the request
    // rather than being applied to a page-local slice.
    await waitFor(() => expect(calls.some((url) => url.includes("status=FAILED"))).toBe(true))
  })

  it("uploads as multipart without forcing a JSON content type", async () => {
    const uploadCalls: RequestInit[] = []
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === "POST") {
        uploadCalls.push(init)
        return jsonResponse(
          {
            items: [
              {
                filename: "ok.docx", outcome: "accepted", resource_id: "r1", status_url: "/api/v1/documents/r1",
                document: queueItem, duplicate_of: null, error: null,
              },
              {
                filename: "again.docx", outcome: "duplicate", resource_id: null, status_url: null,
                document: null, duplicate_of: "r0", error: null,
              },
            ],
            total: 2, accepted: 1, duplicates: 1, rejected: 0,
          },
          202,
        )
      }
      void url
      return jsonResponse({ items: [], page: 1, page_size: 50, total: 0 })
    }))

    renderPage()
    await screen.findByText("还没有简历文件")

    const file = new File(["%PDF-1.7"], "ok.docx", {
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    })
    await userEvent.upload(screen.getByLabelText("选择文件"), file)
    await userEvent.click(screen.getByRole("button", { name: "上传并解析" }))

    expect(await screen.findByText("本批共 2 个文件：受理 1、重复 1、拒绝 0")).toBeInTheDocument()
    expect(screen.getByText("该文件此前已上传，未重复入库")).toBeInTheDocument()

    expect(uploadCalls).toHaveLength(1)
    const init = uploadCalls[0]
    expect(init.body).toBeInstanceOf(FormData)
    // A JSON content type here would break the multipart boundary and the backend
    // would reject an otherwise valid resume.
    const headers = init.headers as Headers
    expect(headers.get("Content-Type")).toBeNull()
  })

  it("surfaces the request id when a retry is rejected", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/retry")) {
        return jsonResponse(
          { code: "NOT_RETRYABLE", message: "当前文档状态不可重试", request_id: "req-retry-9" },
          409,
        )
      }
      void init
      return jsonResponse({ items: [queueItem], page: 1, page_size: 50, total: 1 })
    }))

    renderPage()
    await userEvent.click(await screen.findByRole("button", { name: "重试解析" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("当前文档状态不可重试")
    expect(screen.getByRole("alert")).toHaveTextContent("请求编号：req-retry-9")
  })
})
