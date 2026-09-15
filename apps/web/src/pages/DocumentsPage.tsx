import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { api } from "../api/client"
import type { DocumentBatchAccepted, DocumentStatus } from "../api/types"
import { DocumentQueue, UploadOutcomeList } from "../components/DocumentQueue"
import { ErrorNotice, LoadingState } from "../components/Feedback"
import { documentStatusLabel } from "../lib/documentStatus"
import { useAppStore } from "../state/session"

const STATUS_FILTERS: readonly (DocumentStatus | "ALL")[] = [
  "ALL", "REVIEW_REQUIRED", "READY", "FAILED", "UNSUPPORTED",
]

/**
 * Talent-pool document queue (FIN-004).
 *
 * Upload, watch the parse lifecycle, and retry failures. Three deliberate
 * choices: the status filter is sent to the server (so "还有多少待校对" is a
 * server fact, not a page-local guess), a batch result is shown per file (a
 * duplicate must not hide an accepted sibling), and a failed request renders the
 * backend's error code plus the request id rather than a generic message.
 */
export function DocumentsPage() {
  const token = useAppStore((state) => state.accessToken)!
  const queryClient = useQueryClient()
  const [status, setStatus] = useState<DocumentStatus | "ALL">("ALL")
  const [files, setFiles] = useState<File[]>([])
  const [batch, setBatch] = useState<DocumentBatchAccepted | null>(null)
  const [retryingId, setRetryingId] = useState<string | null>(null)
  const [actionError, setActionError] = useState<unknown>(null)

  const documents = useQuery({
    queryKey: ["documents", status],
    queryFn: () => api.listDocuments(token, { status, pageSize: 50 }),
  })

  const upload = useMutation({
    mutationFn: (selected: File[]) => api.uploadDocuments(token, selected),
    onSuccess: (result) => {
      setBatch(result)
      setActionError(null)
      setFiles([])
      void queryClient.invalidateQueries({ queryKey: ["documents"] })
    },
    onError: (error) => setActionError(error),
  })

  const retry = useMutation({
    mutationFn: (documentId: string) => {
      setRetryingId(documentId)
      return api.retryDocument(token, documentId)
    },
    onSuccess: () => {
      setActionError(null)
      void queryClient.invalidateQueries({ queryKey: ["documents"] })
    },
    onError: (error) => setActionError(error),
    onSettled: () => setRetryingId(null),
  })

  return (
    <section>
      <div className="page-heading">
        <div>
          <p className="eyebrow">Resume intake</p>
          <h1>简历文档</h1>
          <p className="muted">
            上传 PDF 或 DOCX 简历，跟踪解析进度；解析失败会显示具体原因，可重试的单据可直接重新排队。
          </p>
        </div>
      </div>

      <form
        className="panel form-panel"
        onSubmit={(event) => {
          event.preventDefault()
          if (files.length > 0) upload.mutate(files)
        }}
      >
        <div className="section-heading">
          <h2>上传简历</h2>
          <span>支持批量选择，单个文件失败不会影响同批其他文件</span>
        </div>
        <div className="field-grid">
          <div>
            <label htmlFor="document-files">选择文件</label>
            <input
              id="document-files"
              type="file"
              multiple
              accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
              onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
            />
            <small>{files.length > 0 ? `已选择 ${files.length} 个文件` : "尚未选择文件"}</small>
          </div>
        </div>
        <div className="button-row">
          <button className="button button-primary" type="submit" disabled={files.length === 0 || upload.isPending}>
            {upload.isPending ? "上传中…" : "上传并解析"}
          </button>
        </div>
        {upload.isError ? <ErrorNotice error={upload.error} /> : null}
        {batch ? (
          <div className="notice">
            <strong>
              本批共 {batch.total} 个文件：受理 {batch.accepted}、重复 {batch.duplicates}、拒绝 {batch.rejected}
            </strong>
            <UploadOutcomeList items={batch.items} />
          </div>
        ) : null}
      </form>

      <div className="toolbar">
        <label htmlFor="document-status">状态筛选</label>
        <select
          id="document-status"
          value={status}
          onChange={(event) => setStatus(event.target.value as DocumentStatus | "ALL")}
        >
          {STATUS_FILTERS.map((value) => (
            <option key={value} value={value}>
              {value === "ALL" ? "全部" : documentStatusLabel(value)}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="button button-ghost button-small"
          onClick={() => void documents.refetch()}
        >
          刷新
        </button>
      </div>

      {actionError ? <ErrorNotice error={actionError} /> : null}
      {documents.isLoading ? <LoadingState label="正在加载文档队列" /> : null}
      {documents.error ? <ErrorNotice error={documents.error} /> : null}

      {documents.data ? (
        <>
          <p className="muted">
            共 {documents.data.total} 份简历，当前显示 {documents.data.items.length} 份。
            {status === "ALL" ? "" : `筛选条件：${documentStatusLabel(status as DocumentStatus)}。`}
          </p>
          <DocumentQueue
            documents={documents.data.items}
            onRetry={(documentId) => retry.mutate(documentId)}
            retryingId={retryingId}
          />
        </>
      ) : null}
    </section>
  )
}
