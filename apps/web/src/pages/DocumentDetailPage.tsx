import { Link, useParams } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { api } from "../api/client"
import { ErrorNotice, LoadingState } from "../components/Feedback"
import { canRetry, documentStatusLabel, documentStatusTone, failureCategory } from "../lib/documentStatus"
import { useAppStore } from "../state/session"

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
 * Single document view (FIN-004).
 *
 * Exists so a failed parse can be diagnosed without leaving the page: the failure
 * category, the backend's safe message, the retryability flag and the parser
 * version are shown together, and the retry action appears only when the backend
 * said this attempt is retryable.
 */
export function DocumentDetailPage() {
  const { documentId = "" } = useParams()
  const token = useAppStore((state) => state.accessToken)!
  const queryClient = useQueryClient()

  const document = useQuery({
    queryKey: ["document", documentId],
    queryFn: () => api.getDocument(token, documentId),
    enabled: Boolean(documentId),
  })
  const content = useQuery({
    queryKey: ["document-content", documentId],
    queryFn: () => api.getDocumentContent(token, documentId),
    enabled: Boolean(documentId),
    retry: false,
  })

  const retry = useMutation({
    mutationFn: () => api.retryDocument(token, documentId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["document", documentId] })
      void queryClient.invalidateQueries({ queryKey: ["documents"] })
    },
  })

  const item = document.data
  const category = failureCategory(item?.error_code)

  return (
    <section>
      <Link className="back-link" to="/documents">← 返回简历文档</Link>

      <div className="detail-header">
        <div>
          <p className="eyebrow">Resume document</p>
          <h1>{item?.original_filename ?? "文档详情"}</h1>
          <p className="muted">解析状态、失败原因与可重试性，用于判断这份简历为什么没有进入候选人库。</p>
        </div>
        <div className="detail-meta">
          {item ? (
            <span className={`badge ${documentStatusTone(item.status)}`}>{documentStatusLabel(item.status)}</span>
          ) : null}
          {item?.retryable ? <span className="badge badge-warn">可重试</span> : null}
        </div>
      </div>

      {document.isLoading ? <LoadingState label="正在加载文档" /> : null}
      {document.error ? <ErrorNotice error={document.error} /> : null}
      {retry.error ? <ErrorNotice error={retry.error} /> : null}

      {item ? (
        <div className="detail-grid">
          <section className="panel content-panel" aria-label="解析结果">
            <div className="section-heading">
              <h2>解析结果</h2>
              <span>解析器 {item.parser_version ?? "未运行"}</span>
            </div>
            {content.isLoading ? <LoadingState label="正在加载解析内容" /> : null}
            {content.error ? (
              <div className="notice" role="status">
                <strong>原文暂不可用</strong>
                <span>该文档没有可读的解析内容（可能尚未解析成功，或解析内容已不可用）。</span>
              </div>
            ) : null}
            {content.data ? (
              <>
                <dl className="kv">
                  <dt>可定位块</dt>
                  <dd>{content.data.blocks.length}</dd>
                  <dt>页数</dt>
                  <dd>{content.data.page_count ?? "—"}</dd>
                  <dt>段落数</dt>
                  <dd>{content.data.paragraph_count ?? "—"}</dd>
                  <dt>表格数</dt>
                  <dd>{content.data.table_count ?? "—"}</dd>
                </dl>
                {content.data.warnings.length > 0 ? (
                  <div className="notice" role="status">
                    <strong>解析提示</strong>
                    <span>{content.data.warnings.join("；")}</span>
                  </div>
                ) : null}
                <p className="muted">文本摘要：{content.data.full_text.slice(0, 200)}</p>
              </>
            ) : null}
          </section>

          <section className="panel applications-panel" aria-label="文档信息">
            <div className="section-heading">
              <h2>文档信息</h2>
            </div>
            <dl className="kv">
              <dt>类型</dt>
              <dd>{item.media_type}</dd>
              <dt>大小</dt>
              <dd>{formatSize(item.size_bytes)}</dd>
              <dt>尝试次数</dt>
              <dd>第 {item.attempt} 次</dd>
              <dt>内容校验</dt>
              <dd><code>{item.content_sha256.slice(0, 16)}…</code></dd>
              <dt>上传时间</dt>
              <dd>{formatTime(item.created_at)}</dd>
              <dt>更新时间</dt>
              <dd>{formatTime(item.updated_at)}</dd>
            </dl>

            {category ? (
              <div className="notice notice-error" role="alert">
                <strong>{category}</strong>
                {item.error_message ? <span>{item.error_message}</span> : null}
                {item.error_code ? <small>错误代码：{item.error_code}</small> : null}
              </div>
            ) : null}

            <div className="button-row">
              <Link className="button button-primary" to={`/documents/${item.id}/review`}>去校对资料 →</Link>
              {canRetry(item) ? (
                <button
                  type="button"
                  className="button button-ghost"
                  disabled={retry.isPending}
                  onClick={() => retry.mutate()}
                >
                  {retry.isPending ? "提交中…" : "重试解析"}
                </button>
              ) : null}
            </div>
          </section>
        </div>
      ) : null}
    </section>
  )
}
