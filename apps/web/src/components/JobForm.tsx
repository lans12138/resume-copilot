import { useState, type FormEvent } from "react"
import { ApiError } from "../api/client"
import type { Job, JobInput } from "../api/types"

interface Props { initial?: Job; submitLabel: string; busy: boolean; error?: unknown; onSubmit: (input: JobInput) => void; onCancel?: () => void }
const joinSkills = (skills: string[]) => skills.join("、")
const splitSkills = (value: string) => value.split(/[、,，]/).map((item) => item.trim()).filter(Boolean)

function fieldError(error: unknown, field: string) {
  if (!(error instanceof ApiError) || error.status !== 422) return null
  const errors = error.details.errors
  if (!Array.isArray(errors)) return null
  return errors.some((item) => typeof item === "object" && item !== null && "field" in item && (item as { field: unknown }).field === field)
    ? "请检查此字段的格式或取值。" : null
}

export function JobForm({ initial, submitLabel, busy, error, onSubmit, onCancel }: Props) {
  const [title, setTitle] = useState(initial?.title ?? "")
  const [description, setDescription] = useState(initial?.current_version.description ?? "")
  const [requiredSkills, setRequiredSkills] = useState(joinSkills(initial?.current_version.requirements.required_skills ?? []))
  const [preferredSkills, setPreferredSkills] = useState(joinSkills(initial?.current_version.requirements.preferred_skills ?? []))
  const [years, setYears] = useState(initial?.current_version.requirements.minimum_years_experience?.toString() ?? "")
  const [education, setEducation] = useState(initial?.current_version.requirements.education_level ?? "")
  const titleError = fieldError(error, "body.title")
  const descriptionError = fieldError(error, "body.description")
  const requiredSkillsError = fieldError(error, "body.requirements.required_skills")
  const preferredSkillsError = fieldError(error, "body.requirements.preferred_skills")
  const yearsError = fieldError(error, "body.requirements.minimum_years_experience")
  const educationError = fieldError(error, "body.requirements.education_level")

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    onSubmit({ title: title.trim(), description: description.trim(), requirements: {
      required_skills: splitSkills(requiredSkills), preferred_skills: splitSkills(preferredSkills),
      minimum_years_experience: years ? Number(years) : null, education_level: education.trim() || null,
    } })
  }

  return <form className="form-stack" onSubmit={submit}>
    <label htmlFor="job-title">岗位名称</label>
    <input id="job-title" value={title} onChange={(event) => setTitle(event.target.value)} required maxLength={200} aria-invalid={Boolean(titleError)} aria-describedby={titleError ? "job-title-error" : undefined} />
    {titleError ? <small className="field-error" id="job-title-error">{titleError}</small> : null}
    <label htmlFor="job-description">岗位说明</label>
    <textarea id="job-description" value={description} onChange={(event) => setDescription(event.target.value)} required rows={7} maxLength={50000} aria-invalid={Boolean(descriptionError)} aria-describedby={descriptionError ? "job-description-error" : undefined} />
    {descriptionError ? <small className="field-error" id="job-description-error">{descriptionError}</small> : null}
    <div className="field-grid">
      <div><label htmlFor="required-skills">必备技能</label><input id="required-skills" value={requiredSkills} onChange={(event) => setRequiredSkills(event.target.value)} placeholder="Python、PostgreSQL" aria-invalid={Boolean(requiredSkillsError)} aria-describedby={requiredSkillsError ? "required-skills-error" : "required-skills-help"} /><small id="required-skills-help">用逗号或顿号分隔</small>{requiredSkillsError ? <small className="field-error" id="required-skills-error">{requiredSkillsError}</small> : null}</div>
      <div><label htmlFor="preferred-skills">加分技能</label><input id="preferred-skills" value={preferredSkills} onChange={(event) => setPreferredSkills(event.target.value)} placeholder="Docker、Redis" aria-invalid={Boolean(preferredSkillsError)} aria-describedby={preferredSkillsError ? "preferred-skills-error" : undefined} />{preferredSkillsError ? <small className="field-error" id="preferred-skills-error">{preferredSkillsError}</small> : null}</div>
      <div><label htmlFor="experience-years">最低经验年限</label><input id="experience-years" type="number" min="0" max="100" step="0.5" value={years} onChange={(event) => setYears(event.target.value)} aria-invalid={Boolean(yearsError)} aria-describedby={yearsError ? "experience-years-error" : undefined} />{yearsError ? <small className="field-error" id="experience-years-error">{yearsError}</small> : null}</div>
      <div><label htmlFor="education-level">学历要求</label><input id="education-level" value={education} onChange={(event) => setEducation(event.target.value)} placeholder="本科" maxLength={64} aria-invalid={Boolean(educationError)} aria-describedby={educationError ? "education-level-error" : undefined} />{educationError ? <small className="field-error" id="education-level-error">{educationError}</small> : null}</div>
    </div>
    <div className="button-row">
      <button className="button button-primary" type="submit" disabled={busy}>{busy ? "正在提交…" : submitLabel}</button>
      {onCancel ? <button className="button button-ghost" type="button" onClick={onCancel}>取消</button> : null}
    </div>
  </form>
}
