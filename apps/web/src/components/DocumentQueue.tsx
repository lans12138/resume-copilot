import { Link } from "react-router-dom"
import type { DocumentSummary, DocumentUploadItem } from "../api/types"
import {
  canRetry,
  documentStatusLabel,
  documentStatusTone,
  failureCategory,
  uploadOutcomeLabel,
} from "../lib/documentStatus"

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function formatTime(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false })
}

/**
 * The document queue.
 *
 * A failed row shows three separate facts on purpose — the failure category
 * derived from `error_code`, the backend's safe message, and the retry action
 * only when the backend said the attempt is retryable. Collapsing them into one
 * "解析失败" line is exactly what FIN-004 forbids.
 */
export function DocumentQueue({
  documents,
  onRetry,
  retryingId = null,
}: {
  documents: DocumentSummary[]
  onRetry: (documentId: string) => void
  retryingId?: string | null
}) {
  if (documents.length === 0) {
    return (
      <div className="empty-state">
        <strong>还没有简历文件</strong>
        <span>上传 PDF 或 DOCX 简历后，解析进度与失败原因会显示在这里。</span>
      </div>
    )
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">文件名</th>
            <th scope="col">状态</th>
            <th scope="col">尝试</th>
            <th scope="col">失败原因</th>
            <th scope="col">上传时间</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          {documents.map((document) => {
            const category = failureCategory(document.error_code)
            return (
              <tr key={document.id}>
                <td>
                  <Link to={`/documents/${document.id}`}>
                    <strong>{document.original_filename}</strong>
                  </Link>
                  <small>{formatSize(document.size_bytes)}</small>
                </td>
                <td>
                  <span className={`badge ${documentStatusTone(document.status)}`}>
                    {documentStatusLabel(document.status)}
                  </span>
                </td>
                <td>第 {document.attempt} 次</td>
                <td>
                  {category ? (
                    <>
                      <strong>{category}</strong>
                      {document.error_message ? <small>{document.error_message}</small> : null}
                    </>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td>{formatTime(document.created_at)}</td>
                <td>
                  {document.status === "REVIEW_REQUIRED" ? (
                    <Link className="button button-primary button-small" to={`/documents/${document.id}/review`}>
                      去校对
                    </Link>
                  ) : null}
                  {canRetry(document) ? (
                    <button
                      type="button"
                      className="button button-ghost button-small"
                      onClick={() => onRetry(document.id)}
                      disabled={retryingId === document.id}
                    >
                      {retryingId === document.id ? "重试中…" : "重试解析"}
                    </button>
                  ) : null}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/**
 * Per-file upload outcome.
 *
 * A batch is not all-or-nothing: a duplicate or a rejected sibling must not hide
 * the files that were accepted, so every item reports its own outcome and reason.
 */
export function UploadOutcomeList({ items }: { items: DocumentUploadItem[] }) {
  if (items.length === 0) return null
  return (
    <ul className="upload-outcomes">
      {items.map((item) => (
        <li key={`${item.filename}-${item.outcome}-${item.duplicate_of ?? item.resource_id ?? ""}`}>
          <span
            className={`badge ${
              item.outcome === "accepted" ? "badge-ok" : item.outcome === "duplicate" ? "badge-muted" : "badge-fail"
            }`}
          >
            {uploadOutcomeLabel(item.outcome)}
          </span>
          <strong>{item.filename}</strong>
          {item.outcome === "duplicate" ? <small>该文件此前已上传，未重复入库</small> : null}
          {item.error ? (
            <small>
              {failureCategory(item.error.code) ?? item.error.code}：{item.error.message}
            </small>
          ) : null}
        </li>
      ))}
    </ul>
  )
}
