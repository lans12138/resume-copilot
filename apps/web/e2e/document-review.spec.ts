import { fileURLToPath } from "node:url"
import { readFile } from "node:fs/promises"
import path from "node:path"
import { expect, test, type Page } from "@playwright/test"

const FIXTURE = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "fixtures",
  "e2e-candidate-resume.docx",
)
const DOCX_MEDIA_TYPE =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
const FILENAME = "e2e-candidate-resume.docx"
const DEMO_JOB = "[DEMO] 高级后端工程师（Go / Python）"

async function signIn(page: Page) {
  await page.goto("/login")
  await page.getByLabel("用户名").fill("hr.demo")
  await page.getByLabel("密码").fill("demo-password-123")
  await page.getByRole("button", { name: "进入工作台" }).click()
  await expect(page.getByRole("heading", { name: "岗位工作台" })).toBeVisible()
}

/**
 * The worker owns the parse, so the queue is polled rather than assumed. The
 * status filter is not enough on its own: the parse marks the document
 * REVIEW_REQUIRED and the profile draft is written by the follow-up extraction
 * task, so the review screen may still be empty for a moment after 待校对 appears.
 */
async function waitForQueueRow(page: Page, expected: string) {
  const row = page.getByRole("row", { name: new RegExp(FILENAME) })
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await page.getByRole("button", { name: "刷新" }).click()
    if ((await row.textContent())?.includes(expected)) return
    await page.waitForTimeout(1000)
  }
  throw new Error(`The document never reached ${expected}.`)
}

async function waitForDraft(page: Page) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (await page.getByLabel("姓名").isVisible().catch(() => false)) return
    await page.waitForTimeout(1000)
    await page.reload()
  }
  throw new Error("No profile draft was generated for the uploaded resume.")
}

test("HR uploads a resume, pins evidence in the source, and confirms the profile", async ({ page }) => {
  // The queue is polled, not assumed: the worker has to parse the upload and then
  // write the profile draft before the review screen has anything to show.
  test.setTimeout(180_000)

  const pageErrors: string[] = []
  page.on("pageerror", (error) => pageErrors.push(error.message))

  await signIn(page)

  // 1. Upload — the queue must report the file as accepted, not silently swallow it.
  await page.goto("/documents")
  await page.getByLabel("选择文件").setInputFiles({
    name: FILENAME,
    mimeType: DOCX_MEDIA_TYPE,
    buffer: await readFile(FIXTURE),
  })
  await page.getByRole("button", { name: "上传并解析" }).click()
  await expect(page.getByText(/本批共 1 个文件：受理 1/)).toBeVisible()

  // 2. The worker parses it and the extraction task leaves a draft to review.
  await waitForQueueRow(page, "待校对")
  await page.getByRole("row", { name: new RegExp(FILENAME) }).getByRole("link", { name: "去校对" }).click()
  await expect(page.getByRole("heading", { name: FILENAME })).toBeVisible()
  await waitForDraft(page)

  // 3. Writes are job-scoped, so the reviewer picks the job the review is for.
  await page.getByLabel("写操作岗位").selectOption({ label: DEMO_JOB })
  await expect(page.getByRole("button", { name: "确认资料" })).toBeEnabled()

  // 4. Select an excerpt in the source and pin it. The selection is created as a
  //    real DOM range inside the block so the same Range math the app uses in the
  //    browser decides the character offsets — dragging pixels would be flaky.
  await page.evaluate(() => {
    const blocks = Array.from(document.querySelectorAll<HTMLElement>(".source-block"))
    const block = blocks.find((element) => element.textContent?.includes("6 年 Python"))!
    const node = block.querySelector(".source-text")!.firstChild as Text
    const start = node.data.indexOf("Python")
    const range = document.createRange()
    range.setStart(node, start)
    range.setEnd(node, start + "Python 后端".length)
    const selection = window.getSelection()!
    selection.removeAllRanges()
    selection.addRange(range)
    block.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }))
  })

  await expect(page.getByRole("group", { name: "钉住所选证据" })).toContainText("已选 9 个字符")
  await page.getByLabel("证据类别").selectOption("技能")
  await page.getByRole("button", { name: "钉住所选片段" }).click()

  // The excerpt is stored verbatim and the block keeps a visible marker.
  await expect(page.getByText("证据已保存")).toBeVisible()
  await expect(page.locator(".evidence-item .evidence").first()).toHaveText("Python 后端")
  await expect(page.locator("mark.quote").first()).toHaveText("Python 后端")
  await expect(page.getByText("已钉 1 条证据")).toBeVisible()

  // 5. Correct the draft and confirm it: the profile publishes, and the document
  //    follows it to READY in the same transaction (§7.4).
  await page.getByLabel("姓名").fill("张伟")
  await page.getByLabel("工作年限").fill("6")
  await page.getByRole("button", { name: "确认资料" }).click()
  await expect(page.getByText("资料 已就绪")).toBeVisible()
  await expect(page.getByRole("button", { name: "确认资料" })).toBeDisabled()

  await page.getByRole("link", { name: /返回简历文档/ }).click()
  await waitForQueueRow(page, "已就绪")

  expect(pageErrors).toEqual([])
})
