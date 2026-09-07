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

// ---------------------------------------------------------------------------
// Run / Approval / Interview (IMP-026, detailed design §12 / §13)
// ---------------------------------------------------------------------------
export type RunStatus =
  | "CREATED" | "RUNNING" | "WAITING_APPROVAL" | "INTERRUPTED"
  | "COMPLETED" | "FAILED" | "CANCELLED"
export type RunType = "MATCH" | "APPLICATION"
export type AgentEventType =
  | "RUN_CREATED" | "NODE_STARTED" | "NODE_COMPLETED" | "RUN_RESUMED"
  | "STATUS_CHANGED" | "RUN_COMPLETED" | "RUN_FAILED" | "RUN_CANCELLED"

/** Wire envelope for one AgentEvent (detailed design §13.1). */
export interface AgentEventEnvelope {
  event_id: string
  run_id: string
  run_type: RunType
  sequence: number
  event_type: AgentEventType
  node: string | null
  status: string
  message_key: string
  safe_payload: Record<string, unknown>
  occurred_at: string | null
}

export interface MatchRunCandidate {
  candidate_profile_id: string
  application_id: string
  snapshot_order: number
  rrf_score: number
  processing_status: "PENDING" | "COMPLETED" | "FAILED"
  hard_rule_overall: string | null
}
export interface MatchRunDetail {
  run_id: string
  job_id: string
  job_version_id: string
  status: RunStatus
  attempt: number
  rule_version: string
  prompt_version: string
  created_at: string
  finished_at: string | null
  candidates: MatchRunCandidate[]
}
export interface MatchRunSummary {
  run_id: string
  job_id: string
  job_version_id: string
  status: RunStatus
  attempt: number
  created_at: string
}
export interface MatchRunList { runs: MatchRunSummary[] }
export interface RunAccepted { run_id: string; application_id: string; status: string }
export interface MatchRunAccepted { run_id: string; job_id: string; status: string }
export interface CreateMatchRunRequest {
  rule_version?: string
  prompt_version?: string
  retrieval_config?: Record<string, unknown>
  model_config?: Record<string, unknown>
}

export type ApprovalActionType = "UPDATE_APPLICATION_STATUS" | "CREATE_INTERVIEW_SCHEDULE"
export type ApprovalStatus =
  | "PENDING" | "APPROVED" | "EDITED" | "REJECTED" | "EXECUTED" | "EXECUTION_FAILED" | "EXPIRED"
export type InterviewStatus = "SCHEDULED" | "CANCELLED"
export interface ApprovalDetail {
  id: string
  application_run_id: string
  action_type: ApprovalActionType
  status: ApprovalStatus
  original_params: Record<string, unknown> | null
  final_params: Record<string, unknown> | null
  expected_application_version: number | null
  idempotency_key: string
  version: number
  expires_at: string | null
  decided_by: string | null
  decided_at: string | null
}
export type DecisionAction = "APPROVE" | "EDIT" | "REJECT"
export interface DecisionRequest {
  decision: DecisionAction
  expected_version: number
  edited_params?: Record<string, unknown> | null
}

export interface ApplicationRunDetail {
  run_id: string
  application_id: string
  status: RunStatus
  attempt: number
  completion_reason: string | null
  match_report_id: string | null
  question_set: Record<string, unknown> | null
  current_approval: ApprovalDetail | null
  interview_external_id: string | null
  interview_status: InterviewStatus | null
  interview_id: string | null
}
export interface ApplicationRunSummary {
  run_id: string
  application_id: string
  status: RunStatus
  attempt: number
  match_report_id: string | null
}

export interface InterviewDetail {
  id: string
  application_id: string
  run_id: string
  approval_id: string
  external_schedule_id: string
  status: InterviewStatus
  created_at: string
  proposal: {
    application_id: string
    duration_minutes: number
    timezone: string
    interviewer_label: string
  } | null
}
export interface InterviewList { interviews: InterviewDetail[] }

export interface EvidenceOut {
  evidence_chunk_id: string
  quote_text: string
  quote_start: number
  quote_end: number
}
export interface ClaimOut {
  claim_type: string
  claim_text: string
  impact_level: string
  support_level: string
  confidence_note: string | null
  display_order: number
  evidences: EvidenceOut[]
}
export interface ReportOut {
  id: string
  run_id: string
  application_id: string
  candidate_profile_id: string
  overall_score: number
  recommendation: string
  summary: string
  model_snapshot_json: Record<string, unknown>
  created_at: string
  claims: ClaimOut[]
}
export interface ReportList { reports: ReportOut[] }
