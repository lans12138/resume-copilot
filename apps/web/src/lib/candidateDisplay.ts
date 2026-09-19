/**
 * How a candidate is named and summarised in a ranking row or report heading.
 *
 * PORT-005: every one of these screens used to identify a candidate by the first
 * eight characters of a profile UUID, which meant the presenter had to hold a hex
 * string in their head to connect a ranking row to its report and its approval.
 * The name is the identity; the profile id is a tracking detail.
 *
 * Both the name and the summary fields are read *live* from the profile the
 * ranking points at, so a row can legitimately arrive without them (the profile
 * was deleted, or the payload predates this field). Every helper here therefore
 * degrades instead of asserting: a missing name becomes a stated fallback, and a
 * missing field is simply absent from the summary rather than rendered as a
 * misleading "0 年" or an empty gap.
 */

import { EDUCATION_OPTIONS } from "./profileEdit"

/** Shown when the profile behind a ranking row can no longer be read. */
export const UNNAMED_CANDIDATE = "未命名候选人"

const EDUCATION_LABELS = new Map(EDUCATION_OPTIONS.map((option) => [option.value, option.label]))

/** The name to display, falling back to an explicit label rather than an id. */
export function candidateName(name: string | null | undefined): string {
  const trimmed = (name ?? "").trim()
  return trimmed === "" ? UNNAMED_CANDIDATE : trimmed
}

/** Human label for an education enum value; unknown values pass through. */
export function educationLabel(level: string | null | undefined): string | null {
  if (level === null || level === undefined || level === "") return null
  return EDUCATION_LABELS.get(level) ?? level
}

function formatYears(years: number): string {
  return Number.isInteger(years) ? String(years) : years.toFixed(1)
}

/** The summary fields a ranking row and a report heading share. */
export interface CandidateSummaryFields {
  years_experience?: number | null
  education_level?: string | null
  normalized_skills?: string[]
}

/**
 * Short facts about a candidate, in reading order: experience, education, skills.
 *
 * Returns fragments rather than one joined string so each caller controls its own
 * separator, and returns an empty array (not a placeholder) when nothing is known
 * — an empty summary must render as nothing, because "—" next to a name reads like
 * a field that failed to load.
 */
export function candidateFacts(
  candidate: CandidateSummaryFields,
  options: { skillLimit?: number } = {},
): string[] {
  const facts: string[] = []

  if (typeof candidate.years_experience === "number" && Number.isFinite(candidate.years_experience)) {
    facts.push(`${formatYears(candidate.years_experience)} 年经验`)
  }

  const education = educationLabel(candidate.education_level)
  if (education !== null) facts.push(education)

  const skills = (candidate.normalized_skills ?? []).filter((skill) => skill.trim() !== "")
  if (skills.length > 0) {
    const limit = options.skillLimit ?? 4
    const shown = skills.slice(0, limit)
    // Say how many were left out instead of silently truncating: a candidate with
    // twelve skills and one with four must not look identical.
    facts.push(skills.length > limit ? `${shown.join("、")} 等 ${skills.length} 项` : shown.join("、"))
  }

  return facts
}

/** Short form of a profile id, for the secondary "辅助追踪" line. */
export function shortProfileId(id: string): string {
  return id.slice(0, 8)
}
