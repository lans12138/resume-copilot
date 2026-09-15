import { describe, expect, it } from "vitest"
import {
  canRetry,
  documentStatusLabel,
  documentStatusTone,
  failureCategory,
  isTerminal,
  uploadOutcomeLabel,
} from "./documentStatus"

describe("documentStatus", () => {
  it("labels every backend status with a reviewer-facing name", () => {
    expect(documentStatusLabel("REVIEW_REQUIRED")).toBe("待校对")
    expect(documentStatusLabel("FAILED")).toBe("解析失败")
    expect(documentStatusLabel("READY")).toBe("已就绪")
  })

  it("maps each failure code to a specific category", () => {
    expect(failureCategory("UNSUPPORTED_MEDIA")).toBe("文件类型不受支持")
    expect(failureCategory("INVALID_DOCX")).toBe("DOCX 文件损坏")
    expect(failureCategory("UNSAFE_DOCX_CONTENT")).toBe("文档包含宏或嵌入对象")
    expect(failureCategory("STORAGE_UNAVAILABLE")).toBe("存储暂时不可用")
    expect(failureCategory("EMPTY_TEXT")).toContain("扫描件")
  })

  it("surfaces an unknown code instead of collapsing it into a generic message", () => {
    // FIN-004 forbids one catch-all "解析失败": an unmapped code must still reach
    // the reviewer, otherwise a new backend failure mode becomes invisible.
    expect(failureCategory("SOMETHING_NEW")).toBe("未分类解析错误（SOMETHING_NEW）")
  })

  it("returns no category when there is no error code", () => {
    expect(failureCategory(null)).toBeNull()
    expect(failureCategory(undefined)).toBeNull()
    expect(failureCategory("")).toBeNull()
  })

  it("only allows a retry on a retryable terminal failure", () => {
    expect(canRetry({ status: "FAILED", retryable: true })).toBe(true)
    expect(canRetry({ status: "UNSUPPORTED", retryable: true })).toBe(true)
    // The backend said the attempt is permanent: offering a retry would be a lie.
    expect(canRetry({ status: "FAILED", retryable: false })).toBe(false)
    expect(canRetry({ status: "PARSING", retryable: true })).toBe(false)
    expect(canRetry({ status: "REVIEW_REQUIRED", retryable: true })).toBe(false)
  })

  it("treats settled states as terminal", () => {
    expect(isTerminal("REVIEW_REQUIRED")).toBe(true)
    expect(isTerminal("READY")).toBe(true)
    expect(isTerminal("QUEUED")).toBe(false)
    expect(isTerminal("PARSING")).toBe(false)
  })

  it("uses a distinct tone per status group", () => {
    expect(documentStatusTone("READY")).toBe("badge-ok")
    expect(documentStatusTone("FAILED")).toBe("badge-fail")
    expect(documentStatusTone("REVIEW_REQUIRED")).toBe("badge-warn")
  })

  it("labels upload outcomes", () => {
    expect(uploadOutcomeLabel("accepted")).toBe("已受理")
    expect(uploadOutcomeLabel("duplicate")).toBe("重复文件")
    expect(uploadOutcomeLabel("rejected")).toBe("被拒绝")
  })
})
