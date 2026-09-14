import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import type { ReactElement } from "react"
import { MemoryRouter } from "react-router-dom"
import { DocumentQueue, UploadOutcomeList } from "./DocumentQueue"
import type { DocumentSummary } from "../api/types"

// vitest runs without globals, so React Testing Library's auto-cleanup is not
// registered; without this the renders of previous cases leak into the next one.
afterEach(() => cleanup())

/** The queue links each row to the document detail route, so it needs a router. */
function renderQueue(ui: ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>)
}

function documentSummary(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    original_filename: "resume.docx",
    media_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    size_bytes: 2048,
    content_sha256: "a".repeat(64),
    status: "QUEUED",
    parser_version: null,
    attempt: 1,
    retryable: false,
    error_code: null,
    error_message: null,
    uploaded_by: "22222222-2222-4222-8222-222222222222",
    created_at: "2026-09-14T02:00:00Z",
    updated_at: "2026-09-14T02:00:00Z",
    ...overrides,
  }
}

describe("DocumentQueue", () => {
  it("shows an empty state before anything is uploaded", () => {
    renderQueue(<DocumentQueue documents={[]} onRetry={() => {}} />)
    expect(screen.getByText("还没有简历文件")).toBeInTheDocument()
  })

  it("renders the status, attempt count and size of each document", () => {
    renderQueue(<DocumentQueue documents={[documentSummary({ status: "PARSING", attempt: 2 })]} onRetry={() => {}} />)
    expect(screen.getByText("resume.docx")).toBeInTheDocument()
    expect(screen.getByText("解析中")).toBeInTheDocument()
    expect(screen.getByText("第 2 次")).toBeInTheDocument()
    expect(screen.getByText("2.0 KB")).toBeInTheDocument()
  })

  it("shows the failure category and the backend's safe message separately", () => {
    renderQueue(
      <DocumentQueue
        documents={[
          documentSummary({
            status: "FAILED",
            error_code: "EMPTY_TEXT",
            error_message: "未检测到可提取文本，文件可能是扫描件或空文档",
            retryable: false,
          }),
        ]}
        onRetry={() => {}}
      />,
    )
    expect(screen.getByText("未检测到可提取文本（可能是扫描件）")).toBeInTheDocument()
    expect(screen.getByText("未检测到可提取文本，文件可能是扫描件或空文档")).toBeInTheDocument()
  })

  it("offers a retry only when the backend flagged the failure as retryable", () => {
    renderQueue(
      <DocumentQueue
        documents={[
          documentSummary({ status: "FAILED", error_code: "STORAGE_UNAVAILABLE", retryable: true }),
          documentSummary({
            id: "33333333-3333-4333-8333-333333333333",
            original_filename: "scanned.pdf",
            status: "FAILED",
            error_code: "EMPTY_TEXT",
            retryable: false,
          }),
        ]}
        onRetry={() => {}}
      />,
    )
    expect(screen.getAllByRole("button", { name: "重试解析" })).toHaveLength(1)
  })

  it("links a row to its detail page and only offers review for a draft", () => {
    renderQueue(
      <DocumentQueue
        documents={[
          documentSummary({ status: "REVIEW_REQUIRED" }),
          documentSummary({
            id: "44444444-4444-4444-8444-444444444444",
            original_filename: "ready.pdf",
            status: "READY",
          }),
        ]}
        onRetry={() => {}}
      />,
    )

    expect(screen.getByRole("link", { name: "resume.docx" })).toHaveAttribute(
      "href",
      "/documents/11111111-1111-4111-8111-111111111111",
    )
    // A READY document has nothing left to review; re-opening the draft must not be
    // offered as if the review were still pending.
    expect(screen.getAllByRole("link", { name: "去校对" })).toHaveLength(1)
    expect(screen.getByRole("link", { name: "去校对" })).toHaveAttribute(
      "href",
      "/documents/11111111-1111-4111-8111-111111111111/review",
    )
  })

  it("invokes the retry callback with the document id and disables the button while pending", async () => {
    const onRetry = vi.fn()
    const target = documentSummary({ status: "FAILED", error_code: "STORAGE_UNAVAILABLE", retryable: true })
    const { rerender } = renderQueue(<DocumentQueue documents={[target]} onRetry={onRetry} />)

    await userEvent.click(screen.getByRole("button", { name: "重试解析" }))
    expect(onRetry).toHaveBeenCalledWith(target.id)

    rerender(
      <MemoryRouter><DocumentQueue documents={[target]} onRetry={onRetry} retryingId={target.id} /></MemoryRouter>,
    )
    const pending = screen.getByRole("button", { name: "重试中…" })
    expect(pending).toBeDisabled()
  })
})

describe("UploadOutcomeList", () => {
  it("reports each file separately so a duplicate cannot hide an accepted sibling", () => {
    render(
      <UploadOutcomeList
        items={[
          {
            filename: "ok.docx", outcome: "accepted", resource_id: "r1", status_url: "/x",
            document: null, duplicate_of: null, error: null,
          },
          {
            filename: "again.docx", outcome: "duplicate", resource_id: null, status_url: null,
            document: null, duplicate_of: "r0", error: null,
          },
          {
            filename: "bad.exe", outcome: "rejected", resource_id: null, status_url: null,
            document: null, duplicate_of: null,
            error: { code: "UNSUPPORTED_MEDIA", message: "仅支持扩展名、MIME 一致的 PDF 或 DOCX", http_status: 415 },
          },
        ]}
      />,
    )
    expect(screen.getByText("已受理")).toBeInTheDocument()
    expect(screen.getByText("重复文件")).toBeInTheDocument()
    expect(screen.getByText("被拒绝")).toBeInTheDocument()
    expect(screen.getByText("该文件此前已上传，未重复入库")).toBeInTheDocument()
    expect(screen.getByText(/文件类型不受支持：仅支持扩展名/)).toBeInTheDocument()
  })

  it("renders nothing when the batch has no items", () => {
    const { container } = render(<UploadOutcomeList items={[]} />)
    expect(container).toBeEmptyDOMElement()
  })
})
