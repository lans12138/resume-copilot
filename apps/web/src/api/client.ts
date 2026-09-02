import type { ApiErrorPayload, Assignment, AssignmentList, CurrentUser, Job, JobInput, JobList, JobStatus, TokenResponse } from "./types"

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
  if (options.body && !(options.body instanceof URLSearchParams)) headers.set("Content-Type", "application/json")
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
}
