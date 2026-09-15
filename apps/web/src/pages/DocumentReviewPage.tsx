import { useState } from "react"
import { Link, useParams } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ApiError, api } from "../api/client"
import type { CandidateProfileEdit, EvidenceLocator } from "../api/types"
import { EvidencePreview } from "../components/EvidencePreview"
import { ErrorNotice, LoadingState } from "../components/Feedback"
import { ProfileForm, type ProfileFormSubmission } from "../components/ProfileForm"
import { SourceLocatorViewer, type PinRequest } from "../components/SourceLocatorViewer"
import { documentStatusLabel, documentStatusTone } from "../lib/documentStatus"
import { nextChunkIndex } from "../lib/evidence"
import { parseLocator } from "../lib/locator"
import { PROFILE_STATUS_LABELS } from "../lib/profileEdit"
import { useAppStore } from "../state/session"

/**
 * Profile review screen (FIN-004).
 *
 * The reviewer reads the parsed source on the left and edits what will be cited
 * on the right, with the pinned evidence in between. Both writes (pinning a chunk,
 * confirming the draft) are job-scoped on the backend, so the screen asks for the
 * job once and says why: resource-level authorization lives on the job, while the
 * reads here stay document-scoped because a resume in the talent pool is not
 * attached to any job yet.
 *
 * The route element stays mounted when only the document id changes, so the whole
 * screen is keyed by that id: without it a reviewer moving from one resume to the
 * next would find the previous draft in the form and the previous selection in the
 * source viewer.
 */
export function DocumentReviewPage() {
  const { documentId = "" } = useParams()
  return <DocumentReview key={documentId} documentId={documentId} />
}

function DocumentReview({ documentId }: { documentId: string }) {
  const token = useAppStore((state) => state.accessToken)!
  const queryClient = useQueryClient()
  const [jobId, setJobId] = useState("")
  const [activeLocator, setActiveLocator] = useState<EvidenceLocator | null>(null)
  const [actionError, setActionError] = useState<unknown>(null)
  const [pinnedNote, setPinnedNote] = useState<string | null>(null)

  const document = useQuery({
    queryKey: ["document", documentId],
    queryFn: () => api.getDocument(token, documentId),
    enabled: Boolean(documentId),
  })
  const content = useQuery({
    queryKey: ["document-content", documentId],
    queryFn: () => api.getDocumentContent(token, documentId),
    enabled: Boolean(documentId),
  })
  const profile = useQuery({
    queryKey: ["profile-by-document", documentId],
    queryFn: () => api.getProfileByDocument(token, documentId),
    enabled: Boolean(documentId),
  })
  const profileId = profile.data?.id ?? null
  const evidence = useQuery({
    queryKey: ["profile-evidence", profileId],
    queryFn: () => api.listProfileEvidence(token, profileId!),
    enabled: Boolean(profileId),
  })
  const jobs = useQuery({ queryKey: ["jobs", "ALL"], queryFn: () => api.listJobs(token, "ALL") })

  const pin = useMutation({
    mutationFn: (request: PinRequest) =>
      api.pinEvidence(token, jobId, profileId!, [
        {
          document_id: profile.data!.document_id,
          chunk_index: nextChunkIndex(evidence.data ?? []),
          section_type: request.sectionType,
          locator: request.locator,
          text: request.text,
        },
      ]),
    onSuccess: (created) => {
      setActionError(null)
      setPinnedNote(`已钉住「${created[0]?.text ?? ""}」`)
      setActiveLocator(created[0] ? parseLocator(created[0].locator_json) : null)
      void queryClient.invalidateQueries({ queryKey: ["profile-evidence", profileId] })
    },
    onError: (error) => setActionError(error),
  })

  const confirm = useMutation({
    mutationFn: (edit: CandidateProfileEdit) =>
      api.confirmProfile(token, jobId, profileId!, edit, profile.data!.version),
    onSuccess: () => {
      setActionError(null)
      void queryClient.invalidateQueries({ queryKey: ["profile-by-document", documentId] })
      void queryClient.invalidateQueries({ queryKey: ["document", documentId] })
      void queryClient.invalidateQueries({ queryKey: ["documents"] })
    },
    onError: (error) => setActionError(error),
  })

  function submit(submission: ProfileFormSubmission) {
    setActionError(null)
    confirm.mutate(submission.edit)
  }

  const notFound = profile.error instanceof ApiError && profile.error.status === 404
  const evidenceLocators = (evidence.data ?? [])
    .map((chunk) => parseLocator(chunk.locator_json))
    .filter((locator): locator is EvidenceLocator => locator !== null)

  return (
    <section>
      <Link className="back-link" to="/documents">← 返回简历文档</Link>

      <div className="detail-header">
        <div>
          <p className="eyebrow">Profile review</p>
          <h1>{document.data?.original_filename ?? "简历校对"}</h1>
          <p className="muted">
            校对解析出的候选人资料：在原文中钉住证据、修正字段，确认后该资料才会进入检索与报告引用。
          </p>
        </div>
        <div className="detail-meta">
          {document.data ? (
            <span className={`badge ${documentStatusTone(document.data.status)}`}>
              {documentStatusLabel(document.data.status)}
            </span>
          ) : null}
          {profile.data ? (
            <span className="badge badge-neutral">资料 {PROFILE_STATUS_LABELS[profile.data.status]}</span>
          ) : null}
        </div>
      </div>

      <div className="toolbar">
        <label htmlFor="review-job">写操作岗位</label>
        <select id="review-job" value={jobId} onChange={(event) => setJobId(event.target.value)}>
          <option value="">请选择岗位…</option>
          {jobs.data?.items.map((job) => (
            <option key={job.id} value={job.id}>{job.title}</option>
          ))}
        </select>
        <span>钉证据与确认都按岗位做资源级授权，所以需要先选定岗位。</span>
      </div>

      {actionError ? <ErrorNotice error={actionError} /> : null}
      {pinnedNote ? (
        <div className="notice" role="status">
          <strong>证据已保存</strong>
          <span>{pinnedNote}</span>
        </div>
      ) : null}
      {!jobId && profile.data?.status === "REVIEW_REQUIRED" ? (
        <div className="notice" role="status">
          <strong>尚未选择岗位</strong>
          <span>可以继续阅读原文与资料，但钉证据与确认按钮在选定岗位前不可用。</span>
        </div>
      ) : null}

      {content.isLoading ? <LoadingState label="正在加载简历原文" /> : null}
      {content.error ? (
        <>
          <ErrorNotice error={content.error} />
          <p className="muted">原文不可用时仍可查看已抽取的资料与证据，但不能新增证据。</p>
        </>
      ) : null}
      {document.error ? <ErrorNotice error={document.error} /> : null}

      {notFound ? (
        <div className="notice" role="status">
          <strong>该简历尚未生成待校对资料</strong>
          <span>解析成功后会自动生成草稿；若长时间没有草稿，请在文档详情中检查解析状态或重试。</span>
        </div>
      ) : null}
      {profile.error && !notFound ? <ErrorNotice error={profile.error} /> : null}
      {profile.data && evidence.error ? <ErrorNotice error={evidence.error} /> : null}

      <div className="review-grid">
        <div className="review-source">
          {content.data ? (
            <SourceLocatorViewer
              content={content.data}
              activeLocator={activeLocator}
              evidenceLocators={evidenceLocators}
              pinning={pin.isPending}
              pinDisabled={!jobId || !profileId}
              onPin={(request) => {
                setPinnedNote(null)
                pin.mutate(request)
              }}
            />
          ) : null}
        </div>

        <div className="review-side">
          <section className="panel" aria-label="证据">
            <div className="section-heading">
              <h2>证据</h2>
              <span>{evidence.data ? `${evidence.data.length} 条` : "—"}</span>
            </div>
            {evidence.isLoading ? <LoadingState label="正在加载证据" /> : null}
            <EvidencePreview
              chunks={evidence.data ?? []}
              activeLocator={activeLocator}
              onLocate={setActiveLocator}
            />
          </section>

          {profile.data ? (
            <ProfileForm
              profile={profile.data}
              pending={confirm.isPending}
              disabled={profile.data.status !== "REVIEW_REQUIRED" || !jobId}
              disabledReason={
                profile.data.status !== "REVIEW_REQUIRED"
                  ? `该资料当前为「${PROFILE_STATUS_LABELS[profile.data.status]}」，只有待校对的草稿可以确认。`
                  : "请先在上方选定写操作岗位，确认会按岗位做资源级授权。"
              }
              onSubmit={submit}
            />
          ) : null}
        </div>
      </div>
    </section>
  )
}
