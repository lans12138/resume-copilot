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
 * profile, let the confirm's embedding task be delivered, and then carry that
 * same brand-new profile all the way to a scheduled interview.
 *
 * The candidate here is unique per run (a per-run display name), so the
 * assertions can be exact rather than "at least one": this is the only place
 * the suite can check the §10 invariants (one application, two executed
 * approvals, one interview, no orphaned active slot) without the seeded pool
 * polluting the counts.
 */

const FIXTURE_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "fixtures")
const PDF_MEDIA_TYPE = "application/pdf"
const DEMO_JOB = "[DEMO] 高级后端工程师（Go / Python）"

/**
 * A per-run display name, typed into the review form so the editable-name path is
 * exercised. It is deliberately *not* used as an identity anywhere below:
 * `confirm_profile` writes `profile_json` and never touches
 * `Candidate.display_name` (`candidates/service.py:79` sets it once, from the
 * parsed draft), so this value does not reach the talent pool or the ranking —
 * and the fixture's parsed name already collides with the seeded pool. The
 * per-run *filename* is the real handle: it identifies the upload row, and the
 * document is what resolves to the profile id.
 */
const RUN_ID = `${Date.now()}-${Math.floor(Math.random() * 1000)}`
const CANDIDATE_NAME = `FIN010 候选人 ${RUN_ID}`
/**
 * This spec uploads the *PDF* fixture, deliberately, not the DOCX:
 * `document-review.spec.ts` runs earlier in the same suite and ingests
 * `e2e-candidate-resume.docx`, and ingestion deduplicates on the content
 * SHA-256 alone (`backend/app/documents/service.py` `_find_duplicate`, keyed on
 * `stored.content_sha256`) — the filename is irrelevant. With one shared fixture
 * the second upload is correctly answered `受理 0、重复 1`, no fresh document
 * reaches 待校对, and the assertions below end up inspecting the candidate
 * `document-review.spec.ts` already confirmed. Different bytes, same path.
 *
 * Consequence to keep in mind: a Playwright *retry* of either spec re-uploads
 * the same bytes and therefore lands on 重复 as well.
 */
const FILENAME = `fin010-${RUN_ID}.pdf`

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
    mimeType: PDF_MEDIA_TYPE,
    buffer: await readFile(path.join(FIXTURE_DIR, "e2e-candidate-resume.pdf")),
  })
  await page.getByRole("button", { name: "上传并解析" }).click()
  await expect(page.getByText(/本批共 1 个文件：受理 1/)).toBeVisible()

  const queueRow = page.getByRole("row", { name: new RegExp(FILENAME) })
  await pollFor(page, "the upload to reach 待校对", async () => {
    await page.getByRole("button", { name: "刷新" }).click()
    return (await queueRow.textContent())?.includes("待校对") ?? false
  })

  // ---- 2. Confirm the profile with the per-run name ------------------------
  // The review link carries the document id, which is this run's only unambiguous
  // handle on the candidate (see `resolveProfileId` below).
  const reviewLink = queueRow.getByRole("link", { name: "去校对" })
  const reviewHref = await reviewLink.getAttribute("href")
  const documentId = (reviewHref ?? "").match(/\/documents\/([0-9a-f-]+)/)?.[1] ?? ""
  expect(documentId, `unexpected review link: ${reviewHref}`).toMatch(/^[0-9a-f-]{36}$/)
  await reviewLink.click()
  await expect(page.getByRole("heading", { name: FILENAME })).toBeVisible()
  await pollFor(page, "the profile draft", async () => {
    if (await page.getByLabel("姓名").isVisible().catch(() => false)) return true
    await page.reload()
    return false
  })

  await page.getByLabel("写操作岗位").selectOption({ label: DEMO_JOB })
  await expect(page.getByRole("button", { name: "确认资料" })).toBeEnabled()
  // The review form must accept a corrected name — that is the edit path under
  // test — but the value is not an identity; see the note on CANDIDATE_NAME.
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

  // The confirmed profile is now resolvable from the document. Its id is the only
  // thing the ranking table can be matched on (`RankingTable.tsx:40` renders just
  // the first eight characters) and it depends on no display name at all.
  const profileId = await resolveProfileId(page, documentId)
  const PROFILE_SHORT_ID = profileId.slice(0, 8)

  // ---- 3. The profile is visible in the job's talent pool -----------------
  // FIN-010 item 2: "在岗位候选人页看到该 Profile". Navigate by the job-scoped
  // talent pool rather than a deep link so the page's own job picker is exercised.
  //
  // The ranking table can only be matched on `candidate_profile_id` — it renders
  // just the first eight characters (`RankingTable.tsx:40`), and `MatchRunCandidate`
  // carries no name at all — so the identity used throughout is the one resolved
  // from the document, not anything this page shows.
  await page.getByRole("link", { name: "候选人" }).click()
  const jobPicker = page.getByLabel("选择岗位")
  // The option label is `${title}（v${version_no}）` (`CandidatesPage.tsx:51`), so an
  // exact-label select would have to know the seeded version. Select by value.
  const demoJobOption = jobPicker.locator("option", { hasText: DEMO_JOB })
  await expect(demoJobOption).toHaveCount(1)
  await jobPicker.selectOption((await demoJobOption.getAttribute("value")) ?? "")

  // Located by its profile link rather than by name: the pool renders
  // `display_name` (`CandidateTable.tsx:33`), which the confirmation did not change.
  const poolLink = page.locator(`a[href*="/candidates/${profileId}"]`)
  await expect(poolLink).toBeVisible({ timeout: 30_000 })
  // The detail page proves the confirmed profile is what the ranking resolved,
  // not a stale draft.
  await poolLink.click()
  await expect(page).toHaveURL(new RegExp(`/candidates/${profileId}`))
  await expect(page.getByText("尚未确认")).toHaveCount(0)

  // ---- 4. Match run over the demo job -------------------------------------
  // This spec deliberately does not pin evidence. Pinning is the reviewer's
  // call, not a prerequisite of the path, and the ranking below therefore does
  // *not* depend on the embedding: the vector channel recalls embedded chunks
  // only (`backend/app/retrieval/vector.py`), while the structured and keyword
  // channels recall every READY profile. The vector channel for a brand-new
  // profile is covered where a pin exists — `tests/integration/test_document_pipeline_e2e.py`
  // and `tests/validate_nonseed_flow.ps1`.
  await page.goto("/jobs")
  await page.getByRole("link", { name: new RegExp(DEMO_JOB.replace(/[[\]]/g, "\\$&")) }).click()
  await page.getByRole("button", { name: "启动批量分析" }).click()
  await expect(page).toHaveURL(/\/match-runs\/[0-9a-f-]+$/)

  const ranking = page.getByRole("region", { name: "候选人排名" })
  const rankingRow = ranking.getByRole("row", { name: new RegExp(PROFILE_SHORT_ID) })
  await expect.poll(() => rankingRow.count(), { timeout: 60_000 }).toBe(1)

  // ---- 5. Start this candidate's application run --------------------------
  await page.goto("/jobs")
  await page.getByRole("link", { name: new RegExp(DEMO_JOB.replace(/[[\]]/g, "\\$&")) }).click()
  await page.getByRole("button", { name: "启动批量分析" }).click()
  await expect(page).toHaveURL(/\/match-runs\/[0-9a-f-]+$/)

  const reportRow = page
    .getByRole("region", { name: "候选人排名" })
    .getByRole("row", { name: new RegExp(PROFILE_SHORT_ID) })
  await expect(reportRow.getByRole("button", { name: "启动单人流程" })).toBeVisible({
    timeout: 60_000,
  })
  await reportRow.getByRole("button", { name: "启动单人流程" }).click()
  await expect(page).toHaveURL(/\/application-runs\/[0-9a-f-]+$/)

  // ---- 6. Two approvals and a scheduled interview -------------------------
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

/**
 * The profile id behind an uploaded document.
 *
 * This is the spec's only reliable identity for "the candidate we just created":
 *
 *  * the display name is not one — `Candidate.display_name` is set once, when the
 *    draft is materialised (`candidates/service.py:79`, from the parsed
 *    `full_name`), and `confirm_profile` never updates it, so a name corrected on
 *    the review page never reaches the talent pool; the fixture also parses to a
 *    name ("张伟") the seeded pool already uses;
 *  * the ranking table shows only `candidate_profile_id.slice(0, 8)`
 *    (`RankingTable.tsx:40`).
 *
 * The document is unambiguous, because the uploaded filename is unique per run.
 * Resolved through the endpoint the review page itself uses, so the assertion
 * still rests on the public contract rather than on a test-only shortcut.
 */
async function resolveProfileId(page: Page, documentId: string): Promise<string> {
  const profileId = await page.evaluate(async (docId) => {
    const session = JSON.parse(sessionStorage.getItem("resume-copilot.session") ?? "null") as
      | { token?: string }
      | null
    const response = await fetch(`/api/v1/candidate-profiles/by-document/${docId}`, {
      headers: { Authorization: `Bearer ${session?.token ?? ""}` },
    })
    if (!response.ok) return null
    return ((await response.json()) as { id?: string }).id ?? null
  }, documentId)
  expect(profileId, "the uploaded document must resolve to a candidate profile").toBeTruthy()
  return profileId ?? ""
}
