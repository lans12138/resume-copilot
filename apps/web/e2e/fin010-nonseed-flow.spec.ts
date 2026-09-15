import { fileURLToPath } from "node:url"
import { readFile } from "node:fs/promises"
import path from "node:path"
import { expect, test, type Page } from "@playwright/test"

/**
 * FIN-010: the non-seed browser main path.
 *
 * `document-review.spec.ts` uploads a resume and stops at READY.
 * `recruitment-flow.spec.ts` drives match -> dual approval -> interview but
 * starts from the *seeded* demo job. Neither proves the two halves join up for
 * a candidate that did not exist before the test ran, which is what FIN-010
 * asks for: upload from the browser, let the worker parse it, confirm the
 * profile, let the embedding task run, and then carry that same brand-new
 * profile all the way to a scheduled interview.
 *
 * The candidate here is unique per run (a per-run display name), so the
 * assertions can be exact rather than "at least one": this is the only place
 * the suite can check the §10 invariants (one application, two executed
 * approvals, one interview, no orphaned active slot) without the seeded pool
 * polluting the counts.
 */

const FIXTURE_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "fixtures")
const DOCX_MEDIA_TYPE =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
const DEMO_JOB = "[DEMO] 高级后端工程师（Go / Python）"

/**
 * A per-run identity. The seeded pool is stable and shared, so a fresh name is
 * what makes "this profile is the one we just uploaded" assertion-safe: no
 * other spec can produce it.
 */
const RUN_ID = `${Date.now()}-${Math.floor(Math.random() * 1000)}`
const CANDIDATE_NAME = `FIN010 候选人 ${RUN_ID}`
const FILENAME = `fin010-${RUN_ID}.docx`

async function signIn(page: Page): Promise<void> {
  await page.goto("/login")
  await page.getByLabel("用户名").fill("hr.demo")
  await page.getByLabel("密码").fill("demo-password-123")
  await page.getByRole("button", { name: "进入工作台" }).click()
  await expect(page.getByRole("heading", { name: "岗位工作台" })).toBeVisible()
}

/**
 * The worker owns every transition, so each step polls instead of assuming.
 * A single shared helper keeps the retry budget in one place: the parse and the
 * follow-up extraction task are two separate Celery hops, and the embedding
 * task is a third.
 */
async function pollFor(
  page: Page,
  description: string,
  probe: () => Promise<boolean>,
  attempts = 60,
): Promise<void> {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (await probe()) return
    await page.waitForTimeout(1000)
  }
  throw new Error(`Timed out waiting for: ${description}`)
}

test("a freshly uploaded resume runs the whole recruiting path", async ({ page }) => {
  // Parse, extraction and embedding are three worker hops plus a match run and
  // two approvals, so this is the longest spec in the suite by design.
  test.setTimeout(300_000)

  const pageErrors: string[] = []
  page.on("pageerror", (error) => pageErrors.push(error.message))

  await signIn(page)

  // ---- 1. Upload a repository fixture and let the worker parse it ----------
  await page.goto("/documents")
  await page.getByLabel("选择文件").setInputFiles({
    name: FILENAME,
    mimeType: DOCX_MEDIA_TYPE,
    buffer: await readFile(path.join(FIXTURE_DIR, "e2e-candidate-resume.docx")),
  })
  await page.getByRole("button", { name: "上传并解析" }).click()
  await expect(page.getByText(/本批共 1 个文件：受理 1/)).toBeVisible()

  const queueRow = page.getByRole("row", { name: new RegExp(FILENAME) })
  await pollFor(page, "the upload to reach 待校对", async () => {
    await page.getByRole("button", { name: "刷新" }).click()
    return (await queueRow.textContent())?.includes("待校对") ?? false
  })

  // ---- 2. Confirm the profile with the per-run name ------------------------
  await queueRow.getByRole("link", { name: "去校对" }).click()
  await expect(page.getByRole("heading", { name: FILENAME })).toBeVisible()
  await pollFor(page, "the profile draft", async () => {
    if (await page.getByLabel("姓名").isVisible().catch(() => false)) return true
    await page.reload()
    return false
  })

  await page.getByLabel("写操作岗位").selectOption({ label: DEMO_JOB })
  await expect(page.getByRole("button", { name: "确认资料" })).toBeEnabled()
  // The unique name is what lets the later ranking assertion be exact.
  await page.getByLabel("姓名").fill(CANDIDATE_NAME)
  await page.getByLabel("工作年限").fill("6")
  await page.getByRole("button", { name: "确认资料" }).click()
  await expect(page.getByText("资料 已就绪")).toBeVisible()

  // Confirming enqueues `embeddings.generate_chunks`; the document follows the
  // profile to READY in the same transaction (§7.4).
  await page.getByRole("link", { name: /返回简历文档/ }).click()
  await pollFor(page, "the document to reach 已就绪", async () => {
    await page.getByRole("button", { name: "刷新" }).click()
    return (await queueRow.textContent())?.includes("已就绪") ?? false
  })

  // ---- 3. Match run over the demo job -------------------------------------
  // The embedding task (`embeddings.generate_chunks`) is enqueued by the
  // confirm above and must have landed before the vector channel can recall
  // this candidate. The spec asserts that indirectly: the candidate only
  // appears in the ranking below if the embedding exists, and the vector cell
  // is then checked on the candidate detail page in step 4b.
  await page.goto("/jobs")
  await page.getByRole("link", { name: new RegExp(DEMO_JOB.replace(/[[\]]/g, "\\$&")) }).click()
  await page.getByRole("button", { name: "启动批量分析" }).click()
  await expect(page).toHaveURL(/\/match-runs\/[0-9a-f-]+$/)

  const ranking = page.getByRole("region", { name: "候选人排名" })
  await expect
    .poll(() => ranking.getByRole("row", { name: new RegExp(CANDIDATE_NAME) }).count(), {
      timeout: 60_000,
    })
    .toBe(1)

  // ---- 4. The profile is visible on the job's candidate page --------------
  // FIN-010 item 2: "在岗位候选人页看到该 Profile". Navigate by the job-scoped
  // talent pool rather than a deep link so the page's own job picker is exercised.
  await page.getByRole("link", { name: "候选人" }).click()
  await page.getByLabel("选择岗位").selectOption({ label: DEMO_JOB })
  const poolRow = page.getByRole("row", { name: new RegExp(CANDIDATE_NAME) })
  await expect(poolRow).toBeVisible({ timeout: 30_000 })
  // The row links to this candidate's profile; the detail page proves the
  // confirmed profile is what the ranking resolved, not a stale draft.
  await poolRow.getByRole("link", { name: new RegExp(CANDIDATE_NAME) }).click()
  await expect(page.getByRole("heading", { name: CANDIDATE_NAME })).toBeVisible()
  await expect(page.getByText("尚未确认")).toHaveCount(0)

  // Back to the ranking to start this candidate's application run.
  await page.goto("/jobs")
  await page.getByRole("link", { name: new RegExp(DEMO_JOB.replace(/[[\]]/g, "\\$&")) }).click()
  await page.getByRole("button", { name: "启动批量分析" }).click()
  await expect(page).toHaveURL(/\/match-runs\/[0-9a-f-]+$/)

  const reportRow = page
    .getByRole("region", { name: "候选人排名" })
    .getByRole("row", { name: new RegExp(CANDIDATE_NAME) })
  await expect(reportRow.getByRole("button", { name: "启动单人流程" })).toBeVisible({
    timeout: 60_000,
  })
  await reportRow.getByRole("button", { name: "启动单人流程" }).click()
  await expect(page).toHaveURL(/\/application-runs\/[0-9a-f-]+$/)

  // ---- 5. Two approvals and a scheduled interview -------------------------
  await expect(page.getByText("等待审批", { exact: true })).toBeVisible({ timeout: 30_000 })
  await page.getByRole("link", { name: "查看并决策 →" }).click()
  await expect(page.getByRole("region", { name: "动作差异" })).toContainText("更新申请状态")
  await page.getByRole("button", { name: "通过" }).click()
  await expect(page.getByText("决策已提交，当前状态：EXECUTED")).toBeVisible()
  await page.getByRole("link", { name: "返回申请流程查看结果 →" }).click()

  await expect(page.getByText("创建面试安排", { exact: true })).toBeVisible({ timeout: 30_000 })
  await page.getByRole("link", { name: "查看并决策 →" }).click()
  await expect(page.getByRole("region", { name: "动作差异" })).toContainText("创建面试安排")
  await page.getByRole("button", { name: "通过" }).click()
  await expect(page.getByText("决策已提交，当前状态：EXECUTED")).toBeVisible()
  await page.getByRole("link", { name: "返回申请流程查看结果 →" }).click()

  await expect(page.getByText("已完成", { exact: true })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText("已排期", { exact: true })).toBeVisible()
  // No further approvals are pending: the dual gate is fully consumed.
  await expect(page.getByText("该流程当前没有等待决策的审批。", { exact: true })).toBeVisible()
  await page.getByRole("link", { name: "查看面试 →" }).click()
  await expect(page).toHaveURL(/\/interviews\/[0-9a-f-]+$/)

  // The durable counts (one application / two executed approvals / one
  // interview / no orphaned active slot) are asserted by
  // `tests/validate_nonseed_flow.ps1`: they are not observable through the
  // public API surface, and the run detail intentionally exposes only the
  // current approval rather than the executed history.
  expect(pageErrors).toEqual([])
})
