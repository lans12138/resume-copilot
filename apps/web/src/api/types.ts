export type UserRole = "HR" | "HIRING_MANAGER" | "ADMIN"
export type JobStatus = "DRAFT" | "ACTIVE" | "CLOSED"

export interface CurrentUser { id: string; username: string; role: UserRole }
export interface TokenResponse { access_token: string; token_type: "bearer"; expires_in: number }
export interface JobRequirements {
  required_skills: string[]
  preferred_skills: string[]
  minimum_years_experience: number | null
  education_level: string | null
}
export interface JobVersion {
  id: string; version_no: number; description: string; requirements: JobRequirements
  content_sha256: string; schema_version: string; created_at: string
}
export interface Job {
  id: string; title: string; status: JobStatus; version: number; created_by: string
  created_at: string; updated_at: string; current_version: JobVersion
}
export interface JobList { items: Job[]; page: number; page_size: number; total: number }
export interface JobInput { title: string; description: string; requirements: JobRequirements }
export interface Assignment {
  id: string; job_id: string; user_id: string; user_role: "HIRING_MANAGER"
  assigned_by: string; assigned_at: string; revoked_by: string | null; revoked_at: string | null
}
export interface AssignmentList { items: Assignment[]; page: number; page_size: number; total: number }
export interface ApiErrorPayload {
  code?: string; message?: string; request_id?: string; details?: Record<string, unknown>
}
