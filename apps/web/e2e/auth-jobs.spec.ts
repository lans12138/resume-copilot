import { expect, test } from "@playwright/test"

test("HR can log in, create a job, and activate it", async ({ page }) => {
  const pageErrors: string[] = []
  page.on("pageerror", (error) => pageErrors.push(error.message))

  await page.goto("/jobs")
  await expect(page).toHaveURL(/\/login$/)
  await page.getByLabel("用户名").fill("hr-web-demo")
  await page.getByLabel("密码").fill("synthetic-password-123")
  await page.getByRole("button", { name: "进入工作台" }).click()
  await expect(page.getByRole("heading", { name: "岗位工作台" })).toBeVisible()

  await page.getByRole("button", { name: /新建岗位/ }).click()
  await page.getByLabel("岗位名称").fill("平台工程师")
  await page.getByLabel("岗位说明").fill("建设稳定、可观测的招聘基础设施。")
  await page.getByLabel("必备技能").fill("Python、PostgreSQL")
  await page.getByLabel("加分技能").fill("Docker、Redis")
  await page.getByLabel("最低经验年限").fill("3")
  await page.getByLabel("学历要求").fill("本科")
  await page.getByRole("button", { name: "创建并查看" }).click()

  await expect(page.getByRole("heading", { name: "平台工程师" })).toBeVisible()
  await expect(page.getByText("草稿", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "启用岗位" }).click()
  await expect(page.getByText("招聘中", { exact: true })).toBeVisible()
  await expect(page.getByRole("button", { name: "关闭岗位" })).toBeVisible()

  const storage = await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.getItem("resume-copilot.session") }))
  expect(storage.local).toBe(0)
  expect(storage.session).toContain('"token"')
  expect(pageErrors).toEqual([])
})
