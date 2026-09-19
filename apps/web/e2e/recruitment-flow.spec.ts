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
  // PORT-005 made every timeline row render a status badge, so the run status now
  // appears twice on this page: once in the header (`RunStatusBadge`) and once on
  // the row that carried the transition. Scope to the header's metadata row — a
  // bare `getByText` resolves to both, which Playwright's strict mode rejects.
  await expect(
    page.locator(".detail-meta").getByText("等待审批", { exact: true }),
  ).toBeVisible({ timeout: 15000 })

  await page.getByRole("link", { name: "查看并决策 →" }).click()
  // PORT-005 (`e3c072f`) made the approval page's heading the action's own name and
  // left only the parameter diff inside 动作差异, so "审批决策" is no longer a
  // heading and the action name is no longer in that region. The heading now carries
  // both things the two assertions were after: that this is the approval screen, and
  // which action is being authorised.
  await expect(
    page.getByRole("heading", { name: "更新申请状态", level: 1 }),
  ).toBeVisible()
  // The proposal this approval carries, stated by the page itself. The bare
  // `SHORTLISTED` token is no longer addressable: `paramValue` renders a status as
  // "已入围（SHORTLISTED）", and an untouched approval shows that in both diff columns,
  // so a bare `getByText` would resolve to two elements. The action sentence is one
  // element and makes the same claim — which status this decision would apply.
  await expect(
    page.getByRole("region", { name: "拟执行动作" }).getByText("将申请状态更新为「已入围」。"),
  ).toBeVisible()
  await page.getByRole("button", { name: "通过" }).click()
  await expect(page.getByText("决策已提交，当前状态：已执行")).toBeVisible()
  await page.getByRole("link", { name: "返回申请流程查看结果 →" }).click()

  await expect(page.getByText("Question 1", { exact: true })).toBeVisible({ timeout: 15000 })
  await expect(page.getByText("创建面试安排", { exact: true })).toBeVisible()
  await page.getByRole("link", { name: "查看并决策 →" }).click()
  await expect(
    page.getByRole("heading", { name: "创建面试安排", level: 1 }),
  ).toBeVisible()
  await page.getByRole("button", { name: "通过" }).click()
  await expect(page.getByText("决策已提交，当前状态：已执行")).toBeVisible()
  await page.getByRole("link", { name: "返回申请流程查看结果 →" }).click()

  await expect(
    page.locator(".detail-meta").getByText("已完成", { exact: true }),
  ).toBeVisible({ timeout: 15000 })
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
