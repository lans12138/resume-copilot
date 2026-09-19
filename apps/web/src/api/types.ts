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
  /** Live display fields (PORT-005); null when the profile can no longer be read. */
  display_name: string | null
  normalized_skills: string[]
  years_experience: number | null
  education_level: string | null
  /** Source document, for deep-linking a ranking row to the original text. */
  document_id: string | null
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
  /** Server contract is `dict[str, Any]`; narrow with `parseLocator` before use. */
  locator_json: unknown
}
export interface ClaimOut {
  claim_type: string
  claim_text: string
  /** "RULE" is a deterministic verdict; "MODEL" is verified model commentary. */
  source: string
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
  /** Live display fields (PORT-005); null when the profile can no longer be read. */
  display_name: string | null
  document_id: string | null
}
export interface ReportList { reports: ReportOut[] }

/** The model call record for one candidate's report (PORT-003).
 *
 * `reason_code` is the part the report cannot show: whether the model ran at all,
 * and if not, why. Without it, "no model claims" and "the upstream was rate
 * limiting us" look identical. */
export interface ExplanationOut {
  id: string
  run_id: string
  application_id: string
  candidate_profile_id: string
  status: string
  reason_code: string
  summary: string | null
  model: string | null
  prompt_version: string
  rule_version: string
  latency_ms: number
  attempts: number
  prompt_tokens: number | null
  completion_tokens: number | null
  conclusion_count: number
  dropped_conclusion_count: number
  illegal_citation_count: number
  created_at: string
}
export interface ExplanationList { explanations: ExplanationOut[] }

// --- Documents & profile review (FIN-004) ---
export type DocumentStatus =
  | "UPLOADED" | "QUEUED" | "PARSING" | "REVIEW_REQUIRED"
  | "READY" | "FAILED" | "UNSUPPORTED" | "SUPERSEDED"

export interface DocumentSummary {
  id: string
  original_filename: string
  media_type: string
  size_bytes: number
  content_sha256: string
  status: DocumentStatus
  parser_version: string | null
  attempt: number
  retryable: boolean
  // The backend keeps these two fields distinct on purpose: the code drives the
  // UI bucket, the message is the safe human-readable reason. Neither is
  // collapsed into a generic "解析失败".
  error_code: string | null
  error_message: string | null
  uploaded_by: string
  created_at: string
  updated_at: string
}

export interface DocumentList { items: DocumentSummary[]; page: number; page_size: number; total: number }

export type UploadOutcome = "accepted" | "duplicate" | "rejected"

export interface UploadErrorOut { code: string; message: string; http_status: number }

export interface DocumentUploadItem {
  filename: string
  outcome: UploadOutcome
  resource_id: string | null
  status_url: string | null
  document: DocumentSummary | null
  duplicate_of: string | null
  error: UploadErrorOut | null
}

export interface DocumentBatchAccepted {
  items: DocumentUploadItem[]
  total: number
  accepted: number
  duplicates: number
  rejected: number
}

// A locator is the parser's own coordinates back into the source file. The union
// mirrors backend/app/candidates/schemas.py EvidenceLocator so the review screen
// can highlight by structure instead of digging through an untyped blob.
//
// Blocks come from the parser and are re-validated on read, so their locator is
// trustworthy. Evidence chunks are not: the response declares `dict[str, Any]`
// and the demo seed writes its own shape (`{"page", "bbox"}`), so a chunk's
// locator must be narrowed at runtime before it is displayed.
export type EvidenceLocator =
  | { kind: "pdf"; page_number: number; block_index: number; char_start: number; char_end: number }
  | { kind: "docx_paragraph"; paragraph_index: number; char_start: number; char_end: number }
  | {
      kind: "docx_table"
      table_index: number
      row_index: number
      cell_index: number
      char_start: number
      char_end: number
    }

export interface ParsedBlockView { block_index: number; text: string; locator: EvidenceLocator }

export interface DocumentContent {
  document_id: string
  media_type: string
  full_text: string
  page_count: number | null
  paragraph_count: number | null
  table_count: number | null
  warnings: string[]
  blocks: ParsedBlockView[]
}

export type ProfileStatus = "DRAFT" | "REVIEW_REQUIRED" | "READY" | "SUPERSEDED"

export interface CandidateProfile {
  id: string
  candidate_id: string
  document_id: string
  version_no: number
  status: ProfileStatus
  profile_json: Record<string, unknown>
  normalized_skills: string[]
  years_experience: number | null
  education_level: string | null
  schema_version: string
  confirmed_by: string | null
  confirmed_at: string | null
  created_at: string
  updated_at: string
  version: number
}

export interface EvidenceChunk {
  id: string
  document_id: string
  candidate_profile_id: string
  chunk_index: number
  section_type: string
  /** Server contract is `dict[str, Any]`; narrow with `parseLocator` before use. */
  locator_json: unknown
  text: string
  text_sha256: string
  created_at: string
}

export interface EvidenceChunkInput {
  document_id: string
  chunk_index: number
  section_type: string
  locator: EvidenceLocator
  text: string
}

/** Human edits applied to a REVIEW_REQUIRED draft; the backend re-validates it. */
export interface CandidateProfileEdit {
  profile_json: Record<string, unknown>
  normalized_skills: string[]
  years_experience?: number | null
  education_level?: string | null
}


// ---------------------------------------------------------------------------
// Evaluations (FIN-007, detailed design §12.6)
// ---------------------------------------------------------------------------

export type EvaluationStatus = "CREATED" | "RUNNING" | "COMPLETED" | "FAILED"

export type EvaluationKind = "GOLDEN" | "SEMANTIC" | "INJECTION"

export interface DatasetVersionSummary {
  id: string
  name: string
  version: string
  content_hash: string
  schema_version: string
  manifest: Record<string, unknown>
}

/** One stored metric with the bar it was measured against. */
export interface MetricSnapshotView {
  metric_name: string
  metric_value: number
  /** `null` marks an informational metric that does not participate in the gate. */
  threshold: number | null
  passed: boolean
  dimensions: Record<string, unknown>
}

export interface EvaluationRunSummary {
  id: string
  dataset_version_id: string
  kind: EvaluationKind
  status: EvaluationStatus
  config_hash: string
  /** `null` until the run finishes: no verdict exists yet. */
  passed: boolean | null
  error_code: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  failing_metric_count: number
}

export interface EvaluationAccepted {
  evaluation_run_id: string
  status: EvaluationStatus
  kind: EvaluationKind
  dataset_version_id: string
  config_hash: string
}

export interface EvaluationList {
  items: EvaluationRunSummary[]
  total: number
  limit: number
  offset: number
}

export interface EvaluationDetail {
  run: EvaluationRunSummary
  dataset: DatasetVersionSummary
  model_snapshot: Record<string, unknown>
  prompt_versions: Record<string, unknown>
  error_message_safe: string | null
  metrics: MetricSnapshotView[]
}

export interface EvaluationCreateRequest {
  dataset_version_id: string
  kind: EvaluationKind
  config?: Record<string, unknown>
}
