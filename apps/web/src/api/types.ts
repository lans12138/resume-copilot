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

export type HardRuleOutcome = "PASS" | "FAIL" | "UNKNOWN"
export type HardRuleId = "years_experience" | "required_education" | "required_skills"

export interface HardRuleResult {
  rule_id: HardRuleId
  result: HardRuleOutcome
  reason_code: string
  observed_value: unknown | null
  required_value: unknown | null
}

export interface HardRuleBundle {
  rules: HardRuleResult[]
  overall: HardRuleOutcome
}

export type ChannelName = "structured" | "keyword" | "vector"

export interface CandidateRanking {
  candidate_profile_id: string
  snapshot_order: number
  rrf_score: number
  display_name: string
  normalized_skills: string[]
  years_experience: number | null
  education_level: string | null
  structured_rank: number | null
  structured_score: number | null
  keyword_rank: number | null
  keyword_score: number | null
  vector_rank: number | null
  vector_score: number | null
  hard_rule: HardRuleBundle | null
}

export interface CandidateFilter {
  hard_rule?: HardRuleOutcome | "ALL"
  channel?: ChannelName | "ALL"
}

export interface CandidateList {
  job_id: string
  job_version_id: string
  total: number
  items: CandidateRanking[]
  config: {
    top_k: number
    rrf_k: number
    structured_weight: number
    keyword_weight: number
    vector_weight: number
    rule_version: string
  }
}
