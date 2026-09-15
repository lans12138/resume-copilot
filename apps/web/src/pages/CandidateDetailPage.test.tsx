import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { CandidateDetailPage } from "./CandidateDetailPage"
import { useAppStore } from "../state/session"
import type { CandidateProfile, EvidenceChunk, Job } from "../api/types"

const PROFILE_ID = "11111111-1111-4111-8111-111111111111"
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

const profile: CandidateProfile = {
  id: PROFILE_ID,
  candidate_id: "22222222-2222-4222-8222-222222222222",
  document_id: "66666666-6666-4666-8666-666666666666",
  version_no: 1,
  status: "READY",
  profile_json: { full_name: "Demo 林知远" },
  normalized_skills: ["Go", "gRPC", "PostgreSQL"],
  years_experience: 6.5,
  education_level: "MASTER",
  schema_version: "v1",
  confirmed_by: "44444444-4444-4444-8444-444444444444",
  confirmed_at: "2026-09-14T03:00:00Z",
  created_at: "2026-09-14T02:00:00Z",
  updated_at: "2026-09-14T02:00:00Z",
  version: 2,
}

/** One chunk in the parser's locator shape, one in the demo seed's foreign shape. */
const evidence: EvidenceChunk[] = [
  {
    id: "77777777-7777-4777-8777-777777777777",
    document_id: profile.document_id,
    candidate_profile_id: PROFILE_ID,
    chunk_index: 0,
    section_type: "工作经历",
    locator_json: { kind: "pdf", page_number: 1, block_index: 2, char_start: 0, char_end: 30 },
    text: "2021-至今 某电商 高级后端工程师",
    text_sha256: "b".repeat(64),
    created_at: "2026-09-14T02:10:00Z",
  },
  {
    id: "88888888-8888-4888-8888-888888888888",
    document_id: profile.document_id,
    candidate_profile_id: PROFILE_ID,
    chunk_index: 1,
    section_type: "技能",
    locator_json: { page: 2, bbox: [0, 0, 100, 20] },
    text: "精通 Go、gRPC 与 K8s 编排",
    text_sha256: "c".repeat(64),
    created_at: "2026-09-14T02:11:00Z",
  },
]

const rankingItem = {
  candidate_profile_id: PROFILE_ID,
  snapshot_order: 1,
  rrf_score: 0.42,
  display_name: "Demo 林知远",
  normalized_skills: ["Go", "gRPC"],
  years_experience: 6.5,
  education_level: "MASTER",
  structured_rank: 1,
  structured_score: 0.9,
  keyword_rank: null,
  keyword_score: null,
  vector_rank: 2,
  vector_score: 0.81,
  hard_rule: {
    overall: "FAIL" as const,
    rules: [
      {
        rule_id: "required_education" as const,
        result: "FAIL" as const,
        reason_code: "EDUCATION_BELOW_REQUIRED",
        observed_value: "MASTER",
        required_value: "PHD",
      },
    ],
  },
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}

function stubApi(options: { inRanking?: boolean; evidence?: EvidenceChunk[] } = {}) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes("/candidates")) {
      return jsonResponse({
        job_id: JOB_ID,
        job_version_id: job.current_version.id,
        total: options.inRanking === false ? 0 : 1,
        items: options.inRanking === false ? [] : [rankingItem],
        config: {
          top_k: 5, rrf_k: 60, structured_weight: 1, keyword_weight: 1, vector_weight: 1, rule_version: "v1",
        },
      })
    }
    if (url.includes("/evidence")) return jsonResponse(options.evidence ?? evidence)
    if (url.includes(`/profiles/${PROFILE_ID}`)) return jsonResponse(profile)
    if (url.includes("/jobs")) return jsonResponse({ items: [job], page: 1, page_size: 20, total: 1 })
    throw new Error(`Unexpected request: ${url}`)
  }))
}

function renderPage(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/candidates/:profileId" element={<CandidateDetailPage />} />
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

describe("CandidateDetailPage", () => {
  it("asks for a job instead of guessing one", async () => {
    stubApi()
    renderPage(`/candidates/${PROFILE_ID}`)

    expect(await screen.findByText("请选择岗位")).toBeInTheDocument()
    expect(screen.queryByText("资料")).toBeNull()
  })

  it("reads the profile and its hard rules under the job in the URL", async () => {
    stubApi()
    renderPage(`/candidates/${PROFILE_ID}?job=${JOB_ID}`)

    expect(await screen.findByRole("heading", { name: "Demo 林知远" })).toBeInTheDocument()
    expect(screen.getByText("资料 已就绪")).toBeInTheDocument()
    expect(screen.getByText("RRF 0.4200")).toBeInTheDocument()
    expect(screen.getByText("学历要求")).toBeInTheDocument()
    expect(screen.getByText("EDUCATION_BELOW_REQUIRED")).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "查看原文与证据 →" })).toHaveAttribute(
      "href",
      `/documents/${profile.document_id}/review`,
    )
  })

  it("describes the position of a resolvable chunk and admits when it cannot", async () => {
    stubApi()
    renderPage(`/candidates/${PROFILE_ID}?job=${JOB_ID}`)

    expect(await screen.findByText("第 1 页 · 第 2 块 · 字符 0–30")).toBeInTheDocument()
    // The demo seed stores {"page", "bbox"}; that locator is unusable here and must
    // say so rather than render a position that does not exist.
    expect(screen.getByText("位置未知")).toBeInTheDocument()
    expect(screen.getByText("精通 Go、gRPC 与 K8s 编排")).toBeInTheDocument()
  })

  it("says a candidate is outside the Top-K instead of implying no evaluation", async () => {
    stubApi({ inRanking: false })
    renderPage(`/candidates/${PROFILE_ID}?job=${JOB_ID}`)

    expect(await screen.findByText("该候选人不在当前岗位 Top-K 召回结果中，因此没有排名明细。")).toBeInTheDocument()
    expect(screen.getByText("没有可用的硬规则结论。")).toBeInTheDocument()
  })
})
