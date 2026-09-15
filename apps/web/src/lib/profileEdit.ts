import type { CandidateProfile, CandidateProfileEdit, ProfileStatus } from "../api/types"

/**
 * Review-form state and the edit payload sent to confirm.
 *
 * Kept pure and separate from the component so the rules are testable without a
 * browser: the backend re-validates everything the reviewer submits (closed
 * education enum, bounded skill list, non-negative years), so the form must fail
 * in the same places instead of letting a bad value travel and come back as a 422.
 */

export const EDUCATION_OPTIONS: readonly { value: string; label: string }[] = [
  { value: "", label: "未判定" },
  { value: "HIGH_SCHOOL", label: "高中" },
  { value: "ASSOCIATE", label: "大专" },
  { value: "BACHELOR", label: "本科" },
  { value: "MASTER", label: "硕士" },
  { value: "PHD", label: "博士" },
  { value: "OTHER", label: "其他" },
]

export const PROFILE_STATUS_LABELS: Record<ProfileStatus, string> = {
  DRAFT: "草稿",
  REVIEW_REQUIRED: "待校对",
  READY: "已就绪",
  SUPERSEDED: "已被替代",
}

export interface ProfileDraft {
  fullName: string
  skillsText: string
  educationLevel: string
  yearsExperience: string
}

export interface ProfileEditResult {
  /** Null when the draft cannot be submitted; `errors` then explains why. */
  edit: CandidateProfileEdit | null
  errors: { yearsExperience?: string; fullName?: string; skills?: string }
}

const SKILL_SEPARATORS = /[,，、;；\n]+/

/** Skills are typed freely; separators are normalized and duplicates folded. */
export function parseSkillList(text: string): string[] {
  const seen = new Set<string>()
  const skills: string[] = []
  for (const raw of text.split(SKILL_SEPARATORS)) {
    const name = raw.trim()
    if (!name) continue
    const key = name.toLocaleLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    skills.push(name)
  }
  return skills
}

export function formatSkillList(skills: readonly string[]): string {
  return skills.join("、")
}

function existingSkillYears(profile: CandidateProfile): Map<string, number | null> {
  const result = new Map<string, number | null>()
  const skills = profile.profile_json["skills"]
  if (!Array.isArray(skills)) return result
  for (const entry of skills) {
    if (typeof entry !== "object" || entry === null) continue
    const { name, years } = entry as { name?: unknown; years?: unknown }
    if (typeof name !== "string") continue
    result.set(name.toLocaleLowerCase(), typeof years === "number" ? years : null)
  }
  return result
}

export function profileToDraft(profile: CandidateProfile): ProfileDraft {
  const fullName = profile.profile_json["full_name"]
  return {
    fullName: typeof fullName === "string" ? fullName : "",
    skillsText: formatSkillList(profile.normalized_skills),
    educationLevel: profile.education_level ?? "",
    yearsExperience: profile.years_experience === null ? "" : String(profile.years_experience),
  }
}

/** Parse the years field; `undefined` means "leave unset", a string means invalid. */
export function parseYearsExperience(text: string): number | null | string {
  const trimmed = text.trim()
  if (!trimmed) return null
  const value = Number(trimmed)
  if (!Number.isFinite(value) || value < 0 || value > 60) return "工作年限需为 0 到 60 之间的数字"
  return value
}

export function buildProfileEdit(profile: CandidateProfile, draft: ProfileDraft): ProfileEditResult {
  const errors: ProfileEditResult["errors"] = {}

  const fullName = draft.fullName.trim()
  if (fullName.length > 200) errors.fullName = "姓名长度不能超过 200 个字符"

  const years = parseYearsExperience(draft.yearsExperience)
  if (typeof years === "string") errors.yearsExperience = years

  const skills = parseSkillList(draft.skillsText)
  if (skills.length > 200) errors.skills = "技能数量不能超过 200 条"

  if (Object.keys(errors).length > 0) return { edit: null, errors }

  // Only reachable once the field parsed, so the error string can be dropped here.
  const parsedYears = typeof years === "string" ? null : years
  const yearsByName = existingSkillYears(profile)
  const educationLevel = draft.educationLevel.trim() ? draft.educationLevel.trim().toUpperCase() : null

  return {
    edit: {
      profile_json: {
        ...profile.profile_json,
        full_name: fullName || null,
        // Keep the two views of education consistent: the column and the JSON the
        // report cites must not disagree after a human edit.
        education_level: educationLevel,
        skills: skills.map((name) => ({ name, years: yearsByName.get(name.toLocaleLowerCase()) ?? null })),
      },
      normalized_skills: skills,
      years_experience: parsedYears,
      education_level: educationLevel,
    },
    errors,
  }
}
