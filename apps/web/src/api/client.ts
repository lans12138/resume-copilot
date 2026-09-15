import type {
  ApiErrorPayload,
  ApplicationRunDetail,
  ApplicationRunSummary,
  ApprovalDetail,
  Assignment,
  AssignmentList,
  CandidateFilter,
  CandidateList,
  CandidateProfile,
  CandidateProfileEdit,
  CreateMatchRunRequest,
  CurrentUser,
  DecisionRequest,
  DocumentBatchAccepted,
  DocumentContent,
  DocumentList,
  DocumentStatus,
  DocumentSummary,
  EvaluationAccepted,
  EvaluationCreateRequest,
  EvaluationDetail,
  EvaluationKind,
  EvaluationList,
  EvaluationStatus,
  EvidenceChunk,
  EvidenceChunkInput,
  InterviewDetail,
  InterviewList,
  Job,
  JobInput,
  JobList,
  JobStatus,
  MatchRunAccepted,
  MatchRunDetail,
  MatchRunList,
  ReportList,
  RunAccepted,
  TokenResponse,
} from "./types"

const API_BASE_PATH = import.meta.env.VITE_API_BASE_PATH ?? "/api/v1"

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId?: string
  readonly details: Record<string, unknown>
  constructor(status: number, payload: ApiErrorPayload) {
    super(payload.message ?? "请求失败，请稍后重试")
    this.name = "ApiError"
    this.status = status
    this.code = payload.code ?? "UNKNOWN_ERROR"
    this.requestId = payload.request_id
    this.details = payload.details ?? {}
  }
}

async function readPayload(response: Response): Promise<ApiErrorPayload> {
  const contentType = response.headers.get("content-type") ?? ""
  if (!contentType.includes("application/json")) return { code: "INVALID_RESPONSE", message: "服务返回了无法识别的响应" }
  return (await response.json()) as ApiErrorPayload
}

async function request<T>(path: string, options: RequestInit = {}, token?: string): Promise<T> {
  const headers = new Headers(options.headers)
  headers.set("Accept", "application/json")
  if (token) headers.set("Authorization", `Bearer ${token}`)
  if (options.body && !(options.body instanceof URLSearchParams) && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json")
  }
  let response: Response
  try {
    response = await fetch(`${API_BASE_PATH}${path}`, { ...options, headers })
  } catch {
    throw new ApiError(0, { code: "NETWORK_ERROR", message: "无法连接服务，请检查网络或稍后重试" })
  }
  if (!response.ok) throw new ApiError(response.status, await readPayload(response))
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  login(username: string, password: string) {
    return request<TokenResponse>("/auth/token", { method: "POST", body: new URLSearchParams({ username, password }) })
  },
  me: (token: string) => request<CurrentUser>("/auth/me", {}, token),
  listJobs(token: string, status: JobStatus | "ALL") {
    return request<JobList>(`/jobs${status === "ALL" ? "" : `?status=${status}`}`, {}, token)
  },
  createJob: (token: string, input: JobInput) => request<Job>("/jobs", { method: "POST", body: JSON.stringify(input) }, token),
  getJob: (token: string, jobId: string) => request<Job>(`/jobs/${jobId}`, {}, token),
  updateJob(token: string, jobId: string, expectedVersion: number, input: JobInput) {
    return request<Job>(`/jobs/${jobId}`, { method: "PATCH", body: JSON.stringify({ expected_version: expectedVersion, ...input }) }, token)
  },
  transitionJob(token: string, jobId: string, action: "activate" | "close", version: number) {
    return request<Job>(`/jobs/${jobId}/${action}`, { method: "POST", body: JSON.stringify({ expected_version: version }) }, token)
  },
  listAssignments: (token: string, jobId: string) => request<AssignmentList>(`/jobs/${jobId}/assignments`, {}, token),
  grantAssignment: (token: string, jobId: string, userId: string) => request<Assignment>(`/jobs/${jobId}/assignments`, { method: "POST", body: JSON.stringify({ user_id: userId }) }, token),
  revokeAssignment: (token: string, jobId: string, userId: string) => request<void>(`/jobs/${jobId}/assignments/${userId}`, { method: "DELETE" }, token),
  listCandidates: (token: string, jobId: string, filters?: CandidateFilter) => {
    const params = new URLSearchParams()
    if (filters?.hard_rule && filters.hard_rule !== "ALL") params.set("hard_rule", filters.hard_rule)
    if (filters?.channel && filters.channel !== "ALL") params.set("channel", filters.channel)
    const query = params.toString()
    return request<CandidateList>(`/jobs/${jobId}/candidates${query ? `?${query}` : ""}`, {}, token)
  },
  // --- MatchRun (IMP-026) ---
  createMatchRun: (token: string, jobId: string, body: CreateMatchRunRequest = {}) =>
    request<MatchRunAccepted>(`/jobs/${jobId}/match-runs`, { method: "POST", body: JSON.stringify(body) }, token),
  getMatchRun: (token: string, runId: string) =>
    request<MatchRunDetail>(`/match-runs/${runId}`, {}, token),
  listMatchRuns: (token: string, jobId: string) =>
    request<MatchRunList>(`/jobs/${jobId}/match-runs`, {}, token),
  retryMatchRun: (token: string, runId: string) =>
    request<MatchRunAccepted>(`/match-runs/${runId}/retry`, { method: "POST" }, token),
  cancelMatchRun: (token: string, runId: string) =>
    request<MatchRunAccepted>(`/match-runs/${runId}/cancel`, { method: "POST" }, token),
  // --- ApplicationRun (IMP-026) ---
  createApplicationRun: (token: string, applicationId: string, matchReportId?: string) =>
    request<RunAccepted>(
      `/applications/${applicationId}/runs`,
      {
        method: "POST",
        body: JSON.stringify({ match_report_id: matchReportId ?? null }),
      },
      token,
    ),
  getApplicationRun: (token: string, runId: string) =>
    request<ApplicationRunDetail>(`/application-runs/${runId}`, {}, token),
  retryApplicationRun: (token: string, runId: string) =>
    request<RunAccepted>(`/application-runs/${runId}/retry`, { method: "POST" }, token),
  cancelApplicationRun: (token: string, runId: string) =>
    request<RunAccepted>(`/application-runs/${runId}/cancel`, { method: "POST" }, token),
  listJobApplications: (token: string, jobId: string) =>
    request<ApplicationRunSummary[]>(`/jobs/${jobId}/applications`, {}, token),
  // --- Approval (IMP-026) ---
  getApproval: (token: string, approvalId: string) =>
    request<ApprovalDetail>(`/approvals/${approvalId}`, {}, token),
  decideApproval: (token: string, approvalId: string, body: DecisionRequest, idempotencyKey: string) =>
    request<ApprovalDetail>(
      `/approvals/${approvalId}/decision`,
      { method: "POST", body: JSON.stringify(body), headers: { "Idempotency-Key": idempotencyKey } },
      token,
    ),
  // --- Interview (IMP-026) ---
  getInterview: (token: string, interviewId: string) =>
    request<InterviewDetail>(`/interviews/${interviewId}`, {}, token),
  listInterviews: (token: string, jobId: string) =>
    request<InterviewList>(`/interviews?job_id=${jobId}`, {}, token),
  // --- Reports (evidence, IMP-020) ---
  getReports: (token: string, runId: string) =>
    request<ReportList>(`/match-runs/${runId}/reports`, {}, token),
  // --- Documents & profile review (FIN-004) ---
  listDocuments(token: string, options: { status?: DocumentStatus | "ALL"; page?: number; pageSize?: number } = {}) {
    const params = new URLSearchParams()
    if (options.status && options.status !== "ALL") params.set("status", options.status)
    if (options.page) params.set("page", String(options.page))
    if (options.pageSize) params.set("page_size", String(options.pageSize))
    const query = params.toString()
    return request<DocumentList>(`/documents${query ? `?${query}` : ""}`, {}, token)
  },
  getDocument: (token: string, documentId: string) =>
    request<DocumentSummary>(`/documents/${documentId}`, {}, token),
  getDocumentContent: (token: string, documentId: string) =>
    request<DocumentContent>(`/documents/${documentId}/content`, {}, token),
  uploadDocuments(token: string, files: File[]) {
    // The multipart Content-Type must stay unset so the browser adds its own
    // boundary; `request` only sets JSON when the body is not FormData.
    const body = new FormData()
    for (const file of files) body.append("files", file)
    return request<DocumentBatchAccepted>("/documents", { method: "POST", body }, token)
  },
  retryDocument: (token: string, documentId: string) =>
    request<DocumentSummary>(`/documents/${documentId}/retry`, { method: "POST" }, token),
  getProfileByDocument: (token: string, documentId: string) =>
    request<CandidateProfile>(`/candidate-profiles/by-document/${documentId}`, {}, token),
  listProfileEvidence: (token: string, profileId: string) =>
    request<EvidenceChunk[]>(`/candidate-profiles/${profileId}/evidence`, {}, token),
  getJobProfile: (token: string, jobId: string, profileId: string) =>
    request<CandidateProfile>(`/jobs/${jobId}/profiles/${profileId}`, {}, token),
  listJobProfileEvidence: (token: string, jobId: string, profileId: string) =>
    request<EvidenceChunk[]>(`/jobs/${jobId}/profiles/${profileId}/evidence`, {}, token),
  confirmProfile(
    token: string, jobId: string, profileId: string, body: CandidateProfileEdit, expectedVersion: number,
  ) {
    // Writes stay job-scoped on purpose: resource-level authorization lives
    // there, while the review screen's reads are document-scoped.
    return request<CandidateProfile>(
      `/jobs/${jobId}/profiles/${profileId}/confirm?expected_version=${expectedVersion}`,
      { method: "POST", body: JSON.stringify(body) },
      token,
    )
  },
  pinEvidence(token: string, jobId: string, profileId: string, chunks: EvidenceChunkInput[]) {
    return request<EvidenceChunk[]>(
      `/jobs/${jobId}/profiles/${profileId}/evidence`,
      { method: "POST", body: JSON.stringify(chunks) },
      token,
    )
  },
  // Evaluations (FIN-007). The list is filtered server-side: a status filter is a
  // query parameter, not a client-side filter over a fetched page, so paging stays
  // correct once the history outgrows one page.
  listEvaluations(
    token: string,
    options: { status?: EvaluationStatus; kind?: EvaluationKind; page?: number; pageSize?: number } = {},
  ) {
    const params = new URLSearchParams()
    if (options.status) params.set("status", options.status)
    if (options.kind) params.set("kind", options.kind)
    if (options.page !== undefined) params.set("page", String(options.page))
    if (options.pageSize !== undefined) params.set("page_size", String(options.pageSize))
    const query = params.toString()
    return request<EvaluationList>(`/evaluations${query ? `?${query}` : ""}`, {}, token)
  },
  getEvaluation: (token: string, evaluationRunId: string) =>
    request<EvaluationDetail>(`/evaluations/${evaluationRunId}`, {}, token),
  createEvaluation(token: string, body: EvaluationCreateRequest) {
    // 202 Accepted: the run is recorded and queued, not yet scored. A 409
    // EVALUATION_DUPLICATE surfaces as an ApiError the caller renders inline.
    return request<EvaluationAccepted>("/evaluations", { method: "POST", body: JSON.stringify(body) }, token)
  },
}
