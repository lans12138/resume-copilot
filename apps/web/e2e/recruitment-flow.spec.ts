import { expect, test } from "@playwright/test"

test("HR completes match, dual approval, and interview scheduling", async ({ page }) => {
  const pageErrors: string[] = []
  page.on("pageerror", (error) => pageErrors.push(error.message))

  await page.goto("/login")
  await page.getByLabel("用户名").fill("hr.demo")
  await page.getByLabel("密码").fill("demo-password-123")
  await page.getByRole("button", { name: "进入工作台" }).click()

  await expect(page.getByRole("heading", { name: "岗位工作台" })).toBeVisible()
  await page.getByRole("link", { name: /\[DEMO\] 高级后端工程师/ }).click()
  await expect(page.getByRole("heading", { name: "[DEMO] 高级后端工程师（Go / Python）" })).toBeVisible()

  await page.getByRole("button", { name: "启动批量分析" }).click()
  await expect(page).toHaveURL(/\/match-runs\/[0-9a-f-]+$/)
  await expect(page.getByRole("heading", { name: "批量匹配分析" })).toBeVisible()
  const ranking = page.getByRole("region", { name: "候选人排名" })
  // A floor, not an equality: the specs share one seeded database, and
  // document-review.spec.ts sorts first and confirms a resume of its own, so the
  // demo job's ranking legitimately grows past the five seeded candidates. What
  // this test means is "the seeded pool is in the ranking", which is what it asserts.
  // The match run executes in a Celery worker (FIN-005), so candidates only
  // appear after the worker commits and the SSE terminal frame triggers a refetch.
  // That pipeline routinely takes several seconds in CI (worker boot + per-task
  // resource build + retrieval), so give the poll a realistic budget instead of
  // the 5s expect.poll default.
  await expect
    .poll(() => ranking.getByRole("button", { name: "启动单人流程" }).count(), { timeout: 30000 })
    .toBeGreaterThanOrEqual(5)
  const reports = page.getByRole("region", { name: "证据化报告" })
  await expect(reports.getByText("Candidate report").first()).toBeVisible()
  await expect(reports.getByText("证据不足", { exact: true }).first()).toBeVisible()

  await ranking.getByRole("button", { name: "启动单人流程" }).first().click()
  await expect(page).toHaveURL(/\/application-runs\/[0-9a-f-]+$/)
  await expect(page.getByRole("heading", { name: "单人招聘流程" })).toBeVisible()
  // ApplicationRun starts as CREATED and reaches the first approval in Celery.
  await expect(page.getByText("等待审批", { exact: true })).toBeVisible({ timeout: 15000 })

  await page.getByRole("link", { name: "查看并决策 →" }).click()
  await expect(page.getByRole("heading", { name: "审批决策" })).toBeVisible()
  await expect(page.getByRole("region", { name: "动作差异" })).toContainText("更新申请状态")
  await expect(page.getByText("SHORTLISTED", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "通过" }).click()
  await expect(page.getByText("决策已提交，当前状态：EXECUTED")).toBeVisible()
  await page.getByRole("link", { name: "返回申请流程查看结果 →" }).click()

  await expect(page.getByText("Question 1", { exact: true })).toBeVisible({ timeout: 15000 })
  await expect(page.getByText("创建面试安排", { exact: true })).toBeVisible()
  await page.getByRole("link", { name: "查看并决策 →" }).click()
  await expect(page.getByRole("region", { name: "动作差异" })).toContainText("创建面试安排")
  await page.getByRole("button", { name: "通过" }).click()
  await expect(page.getByText("决策已提交，当前状态：EXECUTED")).toBeVisible()
  await page.getByRole("link", { name: "返回申请流程查看结果 →" }).click()

  await expect(page.getByText("已完成", { exact: true })).toBeVisible({ timeout: 15000 })
  await expect(page.getByText(/SUCCESS/)).toBeVisible()
  await expect(page.getByText("该流程当前没有等待决策的审批", { exact: true })).toBeVisible()
  await expect(page.getByText("已排期", { exact: true })).toBeVisible()
  await page.getByRole("link", { name: "查看面试 →" }).click()

  await expect(page).toHaveURL(/\/interviews\/[0-9a-f-]+$/)
  await expect(page.getByRole("heading", { name: "面试安排" })).toBeVisible()
  await expect(page.getByText("已排期", { exact: true })).toHaveCount(2)
  await expect(page.getByText("Question 1", { exact: true })).toBeVisible()
  await expect(page.getByText("45 分钟", { exact: true })).toBeVisible()
  await expect(page.getByText("UTC", { exact: true })).toBeVisible()
  await expect(page.getByText("Hiring Manager", { exact: true })).toBeVisible()
  expect(pageErrors).toEqual([])
})
