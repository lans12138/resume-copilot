import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter, Route, Routes, RouterProvider, createMemoryRouter } from "react-router-dom"
import { DocumentReviewPage } from "./DocumentReviewPage"
import { useAppStore } from "../state/session"
import type { CandidateProfile, DocumentContent, EvidenceChunk, Job, ProfileStatus } from "../api/types"

const DOCUMENT_ID = "11111111-1111-4111-8111-111111111111"
const PROFILE_ID = "22222222-2222-4222-8222-222222222222"
const JOB_ID = "33333333-3333-4333-8333-333333333333"

const job: Job = {
  id: JOB_ID,
  title: "[TEST] 后端工程师",
  status: "ACTIVE",
  version: 1,
  created_by: "44444444-4444-4444-8444-444444444444",
  created_at: "2026-09-14T00:00:00Z",
  updated_at: "2026-09-14T00:00:00Z",
  current_version: {
    id: "55555555-5555-4555-8555-555555555555",
    version_no: 1,
    description: "建设招聘服务。",
    requirements: { required_skills: ["python"], preferred_skills: [], minimum_years_experience: 3, education_level: "本科" },
    content_sha256: "a".repeat(64),
    schema_version: "v1",
    created_at: "2026-09-14T00:00:00Z",
  },
}

const content: DocumentContent = {
  document_id: DOCUMENT_ID,
  media_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  full_text: "张伟\n6 年 Python 后端开发经验",
  page_count: null,
  paragraph_count: 2,
  table_count: null,
  warnings: [],
  blocks: [
    { block_index: 0, text: "张伟", locator: { kind: "docx_paragraph", paragraph_index: 0, char_start: 0, char_end: 2 } },
    {
      block_index: 1,
      text: "6 年 Python 后端开发经验",
      locator: { kind: "docx_paragraph", paragraph_index: 1, char_start: 0, char_end: 17 },
    },
  ],
}

function profile(status: ProfileStatus): CandidateProfile {
  return {
    id: PROFILE_ID,
    candidate_id: "66666666-6666-4666-8666-666666666666",
    document_id: DOCUMENT_ID,
    version_no: 1,
    status,
    profile_json: { full_name: "张伟", skills: [{ name: "Python", years: 6 }], education_level: "BACHELOR" },
    normalized_skills: ["Python"],
    years_experience: 6,
    education_level: "BACHELOR",
    schema_version: "v1",
    confirmed_by: status === "READY" ? "44444444-4444-4444-8444-444444444444" : null,
    confirmed_at: status === "READY" ? "2026-09-14T03:00:00Z" : null,
    created_at: "2026-09-14T02:00:00Z",
    updated_at: "2026-09-14T02:00:00Z",
    version: 3,
  }
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}

interface StubOptions {
  profileStatus?: ProfileStatus
  profileMissing?: boolean
  contentUnavailable?: boolean
  evidence?: EvidenceChunk[]
}

function stubApi(options: StubOptions = {}) {
  const posts: { url: string; body: unknown }[] = []
  let status: ProfileStatus = options.profileStatus ?? "REVIEW_REQUIRED"

  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === "POST") {
      posts.push({ url, body: init.body ? JSON.parse(String(init.body)) : null })
      if (url.includes("/confirm")) {
        status = "READY"
        return jsonResponse(profile(status))
      }
      return jsonResponse(
        [{
          id: "77777777-7777-4777-8777-777777777777",
          document_id: DOCUMENT_ID,
          candidate_profile_id: PROFILE_ID,
          chunk_index: 0,
          section_type: "工作经历",
          locator_json: { kind: "docx_paragraph", paragraph_index: 1, char_start: 0, char_end: 17 },
          text: "6 年 Python 后端开发经验",
          text_sha256: "b".repeat(64),
          created_at: "2026-09-14T04:00:00Z",
        }],
        201,
      )
    }
    if (url.includes("/content")) {
      if (options.contentUnavailable) {
        return jsonResponse({ code: "DOCUMENT_CONTENT_UNAVAILABLE", message: "解析内容不可用", request_id: "req-1" }, 422)
      }
      return jsonResponse(content)
    }
    if (url.includes("/candidate-profiles/by-document/")) {
      if (options.profileMissing) {
        return jsonResponse({ code: "PROFILE_NOT_FOUND", message: "该简历尚未生成待校对资料" }, 404)
      }
      return jsonResponse(profile(status))
    }
    if (url.includes("/evidence")) return jsonResponse(options.evidence ?? [])
    if (url.includes("/jobs")) return jsonResponse({ items: [job], page: 1, page_size: 20, total: 1 })
    if (url.includes(`/documents/${DOCUMENT_ID}`)) {
      return jsonResponse({
        id: DOCUMENT_ID,
        original_filename: "candidate.docx",
        media_type: content.media_type,
        size_bytes: 4096,
        content_sha256: "c".repeat(64),
        status: "REVIEW_REQUIRED",
        parser_version: "parser-v1",
        attempt: 1,
        retryable: false,
        error_code: null,
        error_message: null,
        uploaded_by: "44444444-4444-4444-8444-444444444444",
        created_at: "2026-09-14T02:00:00Z",
        updated_at: "2026-09-14T02:00:00Z",
      })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  return { posts }
}

/** The job list loads asynchronously; the option must exist before it is picked. */
async function selectJob() {
  const select = await screen.findByLabelText("写操作岗位")
  await screen.findByRole("option", { name: job.title })
  await userEvent.selectOptions(select, JOB_ID)
}

function renderPage(entry = `/documents/${DOCUMENT_ID}/review`) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/documents/:documentId/review" element={<DocumentReviewPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** The chunk a report links to when it deep-links into this screen (PORT-005). */
const citedChunk: EvidenceChunk = {
  id: "77777777-7777-4777-8777-777777777777",
  document_id: DOCUMENT_ID,
  candidate_profile_id: PROFILE_ID,
  chunk_index: 0,
  section_type: "工作经历",
  locator_json: { kind: "docx_paragraph", paragraph_index: 1, char_start: 0, char_end: 17 },
  text: "6 年 Python 后端开发经验",
  text_sha256: "b".repeat(64),
  created_at: "2026-09-14T04:00:00Z",
}

beforeEach(() => {
  useAppStore.setState({ accessToken: "test-token", currentUser: null })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  useAppStore.setState({ accessToken: null })
})

describe("DocumentReviewPage", () => {
  it("shows the parsed source, the empty evidence state and the draft side by side", async () => {
    stubApi()
    renderPage()

    expect(await screen.findByText("张伟")).toBeInTheDocument()
    expect(await screen.findByText("还没有钉住的证据")).toBeInTheDocument()
    expect(await screen.findByLabelText("姓名")).toHaveValue("张伟")
  })

  it("keeps the writes blocked until a job is chosen, and says why", async () => {
    stubApi()
    renderPage()

    expect(await screen.findByText("尚未选择岗位")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "确认资料" })).toBeDisabled()
    for (const button of screen.getAllByRole("button", { name: "钉住本块" })) {
      expect(button).toBeDisabled()
    }
  })

  it("pins an excerpt against the document with the next free chunk index", async () => {
    // Chunk 0 is already stored; the next pin must not collide with it, because
    // (document_id, chunk_index) is unique server-side and a clash returns 409.
    const { posts } = stubApi({
      evidence: [{
        id: "88888888-8888-4888-8888-888888888888",
        document_id: DOCUMENT_ID,
        candidate_profile_id: PROFILE_ID,
        chunk_index: 0,
        section_type: "技能",
        locator_json: { kind: "docx_paragraph", paragraph_index: 0, char_start: 0, char_end: 2 },
        text: "张伟",
        text_sha256: "d".repeat(64),
        created_at: "2026-09-14T03:30:00Z",
      }],
    })
    renderPage()

    await selectJob()
    await userEvent.click(screen.getAllByRole("button", { name: "钉住本块" })[1])

    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].url).toContain(`/jobs/${JOB_ID}/profiles/${PROFILE_ID}/evidence`)
    expect(posts[0].body).toEqual([{
      document_id: DOCUMENT_ID,
      chunk_index: 1,
      section_type: "工作经历",
      locator: { kind: "docx_paragraph", paragraph_index: 1, char_start: 0, char_end: 17 },
      text: "6 年 Python 后端开发经验",
    }])
  })

  it("confirms with the version the page loaded so a stale edit is rejected", async () => {
    const { posts } = stubApi()
    renderPage()

    await selectJob()
    await userEvent.clear(screen.getByLabelText("姓名"))
    await userEvent.type(screen.getByLabelText("姓名"), "张伟明")
    await userEvent.click(screen.getByRole("button", { name: "确认资料" }))

    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].url).toContain(`/jobs/${JOB_ID}/profiles/${PROFILE_ID}/confirm?expected_version=3`)
    expect((posts[0].body as { profile_json: Record<string, unknown> }).profile_json["full_name"]).toBe("张伟明")

    // The refetched profile is READY, so the form locks itself instead of offering
    // a second confirm against a stale version.
    expect(await screen.findByText("资料 已就绪")).toBeInTheDocument()
    expect(await screen.findByRole("button", { name: "确认资料" })).toBeDisabled()
  })

  it("surfaces the backend's failure when a confirm is rejected as stale", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === "POST") {
        return jsonResponse(
          { code: "PROFILE_VERSION_CONFLICT", message: "资料已被其他校对者更新", request_id: "req-conflict" },
          409,
        )
      }
      if (url.includes("/content")) return jsonResponse(content)
      if (url.includes("/candidate-profiles/by-document/")) return jsonResponse(profile("REVIEW_REQUIRED"))
      if (url.includes("/evidence")) return jsonResponse([])
      if (url.includes("/jobs")) return jsonResponse({ items: [job], page: 1, page_size: 20, total: 1 })
      return jsonResponse({
        id: DOCUMENT_ID, original_filename: "candidate.docx", media_type: content.media_type, size_bytes: 4096,
        content_sha256: "c".repeat(64), status: "REVIEW_REQUIRED", parser_version: "v1", attempt: 1,
        retryable: false, error_code: null, error_message: null,
        uploaded_by: "44444444-4444-4444-8444-444444444444",
        created_at: "2026-09-14T02:00:00Z", updated_at: "2026-09-14T02:00:00Z",
      })
    }))

    renderPage()
    await selectJob()
    await userEvent.click(screen.getByRole("button", { name: "确认资料" }))

    const alert = await screen.findByRole("alert")
    expect(alert).toHaveTextContent("资料已被其他校对者更新")
    expect(alert).toHaveTextContent("请求编号：req-conflict")
  })

  it("still shows the draft when the parsed content is unavailable", async () => {
    stubApi({ contentUnavailable: true })
    renderPage()

    expect(await screen.findByText("解析内容不可用")).toBeInTheDocument()
    expect(screen.getByText("原文不可用时仍可查看已抽取的资料与证据，但不能新增证据。")).toBeInTheDocument()
    expect(screen.getByLabelText("姓名")).toHaveValue("张伟")
  })

  it("explains a document that has no draft yet", async () => {
    stubApi({ profileMissing: true })
    renderPage()

    expect(await screen.findByText("该简历尚未生成待校对资料")).toBeInTheDocument()
    expect(screen.queryByLabelText("姓名")).toBeNull()
  })

  it("starts clean when the reviewer moves on to the next resume", async () => {
    // The route element stays mounted when only the document id changes, so the
    // draft typed for the first resume must not leak into the second one.
    const secondDocument = "99999999-9999-4999-8999-999999999999"
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const second = url.includes(secondDocument)
      if (init?.method === "POST") return jsonResponse([])
      if (url.includes("/jobs")) return jsonResponse({ items: [job], page: 1, page_size: 20, total: 1 })
      if (url.includes("/evidence")) return jsonResponse([])
      if (url.includes("/content")) {
        return jsonResponse(second ? { ...content, document_id: secondDocument } : content)
      }
      if (url.includes("/candidate-profiles/by-document/")) {
        return jsonResponse(
          second
            ? { ...profile("REVIEW_REQUIRED"), id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", document_id: secondDocument, profile_json: { full_name: "李四" } }
            : profile("REVIEW_REQUIRED"),
        )
      }
      return jsonResponse({
        id: second ? secondDocument : DOCUMENT_ID,
        original_filename: second ? "second.docx" : "candidate.docx",
        media_type: content.media_type,
        size_bytes: 4096,
        content_sha256: "c".repeat(64),
        status: "REVIEW_REQUIRED",
        parser_version: "parser-v1",
        attempt: 1,
        retryable: false,
        error_code: null,
        error_message: null,
        uploaded_by: "44444444-4444-4444-8444-444444444444",
        created_at: "2026-09-14T02:00:00Z",
        updated_at: "2026-09-14T02:00:00Z",
      })
    }))

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const router = createMemoryRouter(
      [{ path: "/documents/:documentId/review", element: <DocumentReviewPage /> }],
      { initialEntries: [`/documents/${DOCUMENT_ID}/review`] },
    )
    render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>)

    await selectJob()
    await userEvent.clear(await screen.findByLabelText("姓名"))
    await userEvent.type(screen.getByLabelText("姓名"), "张伟（误填）")

    await act(async () => {
      await router.navigate(`/documents/${secondDocument}/review`)
    })

    expect(await screen.findByDisplayValue("李四")).toBeInTheDocument()
  })

  // PORT-005: the report links here with ?chunk=<id> so that reading an excerpt and
  // finding its source are one action instead of a manual search.
  it("locates the chunk a report linked to and says which one it is", async () => {
    stubApi({ evidence: [citedChunk] })
    renderPage(`/documents/${DOCUMENT_ID}/review?chunk=${citedChunk.id}`)

    expect(await screen.findByText("已定位到报告引用的原文片段")).toBeInTheDocument()
    expect(screen.getByText(/工作经历 · 第 1 段/)).toBeInTheDocument()

    // Scoped to the source panel: the evidence list shows the same text.
    const source = await screen.findByRole("region", { name: "简历原文" })
    const block = within(source).getByText(citedChunk.text)
    expect(block.closest("[data-block-index]")).toHaveClass("is-active")
  })

  it("does not locate anything without a chunk parameter", async () => {
    stubApi({ evidence: [citedChunk] })
    renderPage()

    const source = await screen.findByRole("region", { name: "简历原文" })
    expect(within(source).getByText(citedChunk.text)).toBeInTheDocument()
    expect(screen.queryByText("已定位到报告引用的原文片段")).toBeNull()
    expect(document.querySelector("[data-block-index].is-active")).toBeNull()
  })

  it("says the cited evidence is from another version instead of highlighting nothing", async () => {
    // A report cites a chunk from the profile as it was when the run happened. If the
    // resume was re-parsed since, that chunk is not in this document's evidence and
    // the link cannot resolve — which the reader has to be told.
    stubApi({ evidence: [] })
    renderPage(`/documents/${DOCUMENT_ID}/review?chunk=${citedChunk.id}`)

    expect(await screen.findByText("报告引用的证据不在当前原文中")).toBeInTheDocument()
    expect(screen.getByText(/可能来自此文档的旧资料版本/)).toBeInTheDocument()
  })
})
