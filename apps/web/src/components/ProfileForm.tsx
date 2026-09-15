import { useState } from "react"
import type { CandidateProfile, CandidateProfileEdit } from "../api/types"
import {
  EDUCATION_OPTIONS,
  buildProfileEdit,
  profileToDraft,
  type ProfileDraft,
} from "../lib/profileEdit"

export interface ProfileFormSubmission {
  edit: CandidateProfileEdit
}

function readString(source: Record<string, unknown>, key: string): string | null {
  const value = source[key]
  return typeof value === "string" && value.trim() ? value : null
}

/**
 * Editable view of an extracted profile draft (FIN-004).
 *
 * The form never sends a half-valid payload: `buildProfileEdit` mirrors the
 * backend's closed education enum, bounded skill list and non-negative years, so
 * a bad value is caught here instead of travelling all the way and returning as a
 * 422. What the reviewer sees confirmed is exactly what the report will cite, so
 * the education column and the `profile_json` view of it are written together.
 */
export function ProfileForm({
  profile,
  onSubmit,
  onReset,
  pending = false,
  disabled = false,
  disabledReason,
}: {
  profile: CandidateProfile
  onSubmit: (submission: ProfileFormSubmission) => void
  onReset?: () => void
  pending?: boolean
  disabled?: boolean
  disabledReason?: string
}) {
  const [draft, setDraft] = useState<ProfileDraft>(() => profileToDraft(profile))
  const [errors, setErrors] = useState<ReturnType<typeof buildProfileEdit>["errors"]>({})

  const summary = readString(profile.profile_json, "summary")

  function update<K extends keyof ProfileDraft>(key: K, value: ProfileDraft[K]) {
    setDraft((current) => ({ ...current, [key]: value }))
  }

  return (
    <form
      className="panel profile-panel"
      aria-label="候选人资料校对"
      onSubmit={(event) => {
        event.preventDefault()
        const result = buildProfileEdit(profile, draft)
        setErrors(result.errors)
        if (result.edit) onSubmit({ edit: result.edit })
      }}
    >
      <div className="section-heading">
        <h2>候选人资料</h2>
        <span>第 {profile.version_no} 版 · schema {profile.schema_version}</span>
      </div>

      {summary ? <p className="muted">{summary}</p> : null}

      <div className="field-grid">
        <div>
          <label htmlFor="profile-full-name">姓名</label>
          <input
            id="profile-full-name"
            value={draft.fullName}
            disabled={disabled}
            onChange={(event) => update("fullName", event.target.value)}
          />
          {errors.fullName ? <small className="field-error" role="alert">{errors.fullName}</small> : null}
        </div>
        <div>
          <label htmlFor="profile-years">工作年限</label>
          <input
            id="profile-years"
            inputMode="decimal"
            value={draft.yearsExperience}
            disabled={disabled}
            onChange={(event) => update("yearsExperience", event.target.value)}
          />
          {errors.yearsExperience ? <small className="field-error" role="alert">{errors.yearsExperience}</small> : null}
        </div>
        <div>
          <label htmlFor="profile-education">学历</label>
          <select
            id="profile-education"
            value={draft.educationLevel}
            disabled={disabled}
            onChange={(event) => update("educationLevel", event.target.value)}
          >
            {EDUCATION_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="profile-skills">技能（用、或逗号分隔）</label>
          <input
            id="profile-skills"
            value={draft.skillsText}
            disabled={disabled}
            onChange={(event) => update("skillsText", event.target.value)}
          />
          {errors.skills ? <small className="field-error" role="alert">{errors.skills}</small> : null}
        </div>
      </div>

      <div className="button-row">
        <button className="button button-primary" type="submit" disabled={disabled || pending}>
          {pending ? "提交中…" : "确认资料"}
        </button>
        <button
          type="button"
          className="button button-ghost"
          disabled={disabled}
          onClick={() => {
            setDraft(profileToDraft(profile))
            setErrors({})
            onReset?.()
          }}
        >
          重置为当前资料
        </button>
      </div>

      {disabled ? (
        <p className="muted">{disabledReason ?? "当前资料不在待校对状态，无法再次确认。"}</p>
      ) : null}
      {profile.status === "READY" && profile.confirmed_at ? (
        <p className="muted">已于 {new Date(profile.confirmed_at).toLocaleString("zh-CN", { hour12: false })} 确认。</p>
      ) : null}
    </form>
  )
}
