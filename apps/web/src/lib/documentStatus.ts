import type { DocumentStatus } from "../api/types"

/**
 * Presentation mapping for the document queue.
 *
 * FIN-004 requires the queue to surface the *reason* a parse failed instead of one
 * generic message, so the mapping below is deliberately explicit per backend code.
 * An unknown code is never swallowed: it falls through to the raw code, which is
 * more useful to a reviewer than "解析失败".
 */

const STATUS_LABELS: Record<DocumentStatus, string> = {
  UPLOADED: "已上传",
  QUEUED: "排队中",
  PARSING: "解析中",
  REVIEW_REQUIRED: "待校对",
  READY: "已就绪",
  FAILED: "解析失败",
  UNSUPPORTED: "不支持",
  SUPERSEDED: "已被替代",
}

/** Terminal states never change again without a new attempt. */
const TERMINAL: readonly DocumentStatus[] = ["REVIEW_REQUIRED", "READY", "FAILED", "UNSUPPORTED", "SUPERSEDED"]

const FAILURE_CATEGORIES: Record<string, string> = {
  UNSUPPORTED_MEDIA: "文件类型不受支持",
  ENCRYPTED_PDF: "加密 PDF 无法解析",
  ENCRYPTED_ARCHIVE: "加密文档无法解析",
  INVALID_PDF: "PDF 文件损坏",
  INVALID_DOCX: "DOCX 文件损坏",
  SIGNATURE_MISMATCH: "文件签名与声明类型不一致",
  EMPTY_TEXT: "未检测到可提取文本（可能是扫描件）",
  UNSAFE_DOCX_CONTENT: "文档包含宏或嵌入对象",
  UNSAFE_ARCHIVE_PATH: "文档包含不安全路径",
  FILE_TOO_LARGE: "文件超过大小限制",
  ARCHIVE_LIMIT_EXCEEDED: "解压规模超出安全限制",
  PDF_PAGE_LIMIT_EXCEEDED: "PDF 页数超过限制",
  EXTRACTED_TEXT_LIMIT_EXCEEDED: "提取文本长度超过限制",
  PARSER_TIMEOUT: "解析超时",
  EMPTY_FILE: "不接受空文件",
  INVALID_FILENAME: "文件名无效",
  STORAGE_UNAVAILABLE: "存储暂时不可用",
}

export function documentStatusLabel(status: DocumentStatus): string {
  return STATUS_LABELS[status] ?? status
}

export function documentStatusTone(status: DocumentStatus): string {
  if (status === "READY") return "badge-ok"
  if (status === "FAILED" || status === "UNSUPPORTED") return "badge-fail"
  if (status === "REVIEW_REQUIRED") return "badge-warn"
  if (status === "SUPERSEDED") return "badge-muted"
  return "badge-info"
}

/** Human-readable failure bucket; unknown codes surface verbatim, never hidden. */
export function failureCategory(errorCode: string | null | undefined): string | null {
  if (!errorCode) return null
  return FAILURE_CATEGORIES[errorCode] ?? `未分类解析错误（${errorCode}）`
}

export function isTerminal(status: DocumentStatus): boolean {
  return TERMINAL.includes(status)
}

/** A retry only makes sense for a terminal failure the backend flagged retryable. */
export function canRetry(document: { status: DocumentStatus; retryable: boolean }): boolean {
  return document.retryable && (document.status === "FAILED" || document.status === "UNSUPPORTED")
}

/** Upload rejection reasons come from the same vocabulary as parse failures. */
export function uploadOutcomeLabel(outcome: "accepted" | "duplicate" | "rejected"): string {
  return outcome === "accepted" ? "已受理" : outcome === "duplicate" ? "重复文件" : "被拒绝"
}
