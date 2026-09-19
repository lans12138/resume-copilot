import { expect, test, type Page } from "@playwright/test"
import {
  DEMO_JOB,
  DEMO_MANAGER_USERNAME,
  failWith,
  openJob,
  pollFor,
  signIn,
  signInAs,
  streamFrames,
  sseAuthRevoked,
  sseEvent,
  sseHeartbeat,
} from "./fin011-helpers"

/**
 * FIN-011: the failure and safety matrix.
 *
 * §19.2 records the gap this closes: "尚未通过 Nginx 的 Bearer SSE 全栈探针，
 * 撤权和断线恢复缺浏览器级验证". The backend behaviours are already unit-tested
 * — 8 approval tests, 9 SSE tests, including mid-stream revocation with event
 * withholding (`test_sse.py::test_midstream_revocation_closes_stream`). What no
 * test covered is the *browser* half: that the React client turns each failure
 * into a specific, recoverable UI state rather than a dead page.
 *
 * Real-path first: where the live stack can produce the failure on its own, the
 * spec drives it for real. Only infrastructure failures (a dependency 503, a
 * lost Redis notification, a mid-stream revocation that needs a second actor)
 * are injected, and those are titled `INJECTED:`.
 *
 * The Nginx-proxied Bearer SSE probe is FIN-012 item 2, not here.
 */

test.describe("FIN-011 approval decisions", () => {
  test("a rejected approval completes the run without scheduling an interview", async ({ page }) => {
    test.setTimeout(180_000)
    const pageErrors: string[] = []
    page.on("pageerror", (error) => pageErrors.push(error.message))

    await signIn(page)
    await startApplicationRun(page)

    // First gate: reject instead of approve. A rejection is a *business*
    // conclusion, not an engineering failure, so the run completes with
    // completion_reason=ACTION_REJECTED (approvals/service.py:285-291) and must
    // never advance to the interview-schedule gate.
    await decideOnCurrentApproval(page, "驳回")
    await expect(page.getByText("决策已提交，当前状态：REJECTED")).toBeVisible()
    await page.getByRole("link", { name: "返回申请流程查看结果 →" }).click()

    // PORT-005 made every timeline row render a status badge, so the run status now
    // appears twice on this page: once in the header (`RunStatusBadge`) and once on
    // the terminal timeline row. Scope to the header's metadata row — a bare
    // `getByText` resolves to both, which Playwright's strict mode rejects.
    await expect(
      page.locator(".detail-meta").getByText("已完成", { exact: true }),
    ).toBeVisible({ timeout: 30_000 })
    // The reason is rendered inside a sentence — `ApplicationRunPage.tsx:48` prints
    // "申请 {id} · 尝试 {n} · {completion_reason}" as one text run — so no element's
    // *whole* text is the bare token and `{ exact: true }` can never match it.
    await expect(page.getByText(/ACTION_REJECTED/)).toBeVisible()
    await expect(page.getByText("创建面试安排", { exact: true })).toHaveCount(0)
    await expect(page.getByText("尚未创建面试安排。", { exact: true })).toBeVisible()
    expect(pageErrors).toEqual([])
  })

  test("an edited approval submits the edited parameters, not the original", async ({ page }) => {
    test.setTimeout(180_000)
    await signIn(page)
    await startApplicationRun(page)

    await openCurrentApproval(page)
    await expect(page.getByRole("region", { name: "动作差异" })).toContainText("更新申请状态")
    await expect(page.getByText("SHORTLISTED", { exact: true })).toBeVisible()

    // EDIT is a distinct decision path from APPROVE: `ApprovalPage` sends
    // decision=EDIT with `edited_params` as soon as the edit form has applied
    // (ApprovalPage.tsx:70, `editedParams !== null ? "EDIT" : "APPROVE"`).
    // Change the target status to a *different* legal value (ON_HOLD is in
    // ApplicationStatus), so the assertion proves the edit took effect rather
    // than an unchanged params object being resubmitted as EDIT.
    await page.getByRole("button", { name: "修改参数" }).click()
    const editor = page.getByRole("region", { name: "编辑参数" })
    await expect(editor).toBeVisible()
    await editor.getByLabel("编辑后参数（JSON）").fill('{"target_status": "ON_HOLD"}')
    // EditableParamsForm's submit is labelled 应用修改 (EditableParamsForm.tsx:37).
    await editor.getByRole("button", { name: "应用修改" }).click()
    await expect(editor).toHaveCount(0)
    // The diff now previews the edited value, which is what will be submitted.
    await expect(page.getByRole("region", { name: "动作差异" })).toContainText("ON_HOLD")

    await page.getByRole("button", { name: "通过" }).click()
    await expect(page.getByText("决策已提交，当前状态：EXECUTED")).toBeVisible()
  })

  test("deciding the same approval twice is refused with 409, not a second effect", async ({ page }) => {
    test.setTimeout(180_000)
    await signIn(page)
    await startApplicationRun(page)

    await openCurrentApproval(page)
    await page.getByRole("button", { name: "通过" }).click()
    await expect(page.getByText("决策已提交，当前状态：EXECUTED")).toBeVisible()

    // Re-submitting the same decision is a real duplicate request against a
    // now-non-PENDING approval: `ApprovalService.decide` raises
    // APPROVAL_ALREADY_DECIDED (409) — decided exactly once
    // (approvals/service.py:247). Reload so the page reads the decided fact and
    // the controls are gone; the guard is what must hold, not a UI trick.
    await page.reload()
    await expect(page.getByText("该审批已不可决策", { exact: true })).toBeVisible()
    // `ApprovalPage.tsx:101` renders the status as a sentence with a full stop
    // ("当前状态：EXECUTED。"), so the bare label is not an exact match.
    await expect(page.getByText(/当前状态：EXECUTED/)).toBeVisible()
    await expect(page.getByRole("button", { name: "通过" })).toHaveCount(0)

    // The durable proof that the second attempt changed nothing is that the run
    // has exactly one interview slot at the end; that count is asserted in
    // tests/validate_nonseed_flow.ps1 for the same reason (no read model for it).
  })

  test("INJECTED: a version conflict refreshes the fact and offers a deliberate retry", async ({ page }) => {
    test.setTimeout(180_000)
    await signIn(page)
    await startApplicationRun(page)
    await openCurrentApproval(page)

    // The live stack cannot easily be made to race itself from one browser, so
    // the 409 the optimistic lock produces (approvals/service.py:255) is
    // injected on the decision POST only. Everything around it is real.
    await page.route("**/api/v1/approvals/*/decision", (route) =>
      failWith(route, 409, "VERSION_CONFLICT", "审批版本冲突，请刷新后重试"),
    )
    await page.getByRole("button", { name: "通过" }).click()

    // ApprovalPage treats 409 as stale, re-reads the approval, and asks the user
    // to confirm again rather than silently replaying (ApprovalPage.tsx:53-59).
    await expect(
      page.getByText("页面数据已发生变化（可能已过期或版本冲突）", { exact: true }),
    ).toBeVisible()
    await expect(page.getByText("已刷新为最新事实，请确认后再决策。", { exact: true })).toBeVisible()
    await expect(page.getByRole("button", { name: "通过" })).toBeEnabled()

    // Recoverable: unblocking the route lets the same session complete normally.
    await page.unroute("**/api/v1/approvals/*/decision")
    await page.getByRole("button", { name: "通过" }).click()
    await expect(page.getByText("决策已提交，当前状态：EXECUTED")).toBeVisible()
  })

  test("INJECTED: an expired approval reports expiry instead of a generic failure", async ({ page }) => {
    test.setTimeout(180_000)
    await signIn(page)
    await startApplicationRun(page)
    await openCurrentApproval(page)

    await page.route("**/api/v1/approvals/*/decision", (route) =>
      failWith(route, 409, "APPROVAL_EXPIRED", "审批已过期"),
    )
    await page.getByRole("button", { name: "通过" }).click()
    await expect(
      page.getByText("页面数据已发生变化（可能已过期或版本冲突）", { exact: true }),
    ).toBeVisible()
  })
})

test.describe("FIN-011 request failures and recovery", () => {
  test("INJECTED: a 403 explains the permission change and keeps the page usable", async ({ page }) => {
    test.setTimeout(180_000)
    await signIn(page)

    // `/jobs` is requested without a query string when the filter is ALL (the
    // default: `state/session.ts` jobStatusFilter="ALL", `client.ts:89` appends
    // a query only for a concrete status). The pattern must therefore match both
    // list forms and still reject `/jobs/{id}` sub-resources.
    await page.route(/\/api\/v1\/jobs(\?.*)?$/, (route) =>
      failWith(route, 403, "FORBIDDEN", "无权访问该岗位", "req-e2e-403"),
    )
    await page.goto("/jobs")

    const alert = page.getByRole("alert")
    await expect(alert).toContainText("当前账号没有执行此操作的权限。")
    await expect(alert).toContainText("无权访问该岗位")
    // The request id must reach the user: it is the only handle support has.
    await expect(alert).toContainText("请求编号：req-e2e-403")
    // Recoverable, not fatal: the shell is still there.
    await expect(page.getByRole("heading", { name: "岗位工作台" })).toBeVisible()
  })

  test("INJECTED: a 503 says the dependency is unavailable, not that input was wrong", async ({ page }) => {
    test.setTimeout(180_000)
    await signIn(page)

    await page.route(/\/api\/v1\/jobs(\?.*)?$/, (route) =>
      failWith(route, 503, "DEPENDENCY_UNAVAILABLE", "检索服务暂时不可用", "req-e2e-503"),
    )
    await page.goto("/jobs")

    const alert = page.getByRole("alert")
    // 503 and 422 must not share wording: one is retry-later, the other is fix-input.
    await expect(alert).toContainText("核心依赖暂时不可用，请稍后重试。")
    await expect(alert).toContainText("请求编号：req-e2e-503")
  })

  test("INJECTED: a 422 reports a validation problem and surfaces the request id", async ({ page }) => {
    test.setTimeout(180_000)
    await signIn(page)

    await page.route(/\/api\/v1\/jobs(\?.*)?$/, (route) =>
      failWith(route, 422, "VALIDATION_ERROR", "筛选参数不合法", "req-e2e-422"),
    )
    await page.goto("/jobs")

    const alert = page.getByRole("alert")
    await expect(alert).toContainText("提交内容未通过校验，请检查各字段。")
    await expect(alert).toContainText("请求编号：req-e2e-422")
  })

  test("INJECTED: a 401 while reading a run drops the session and returns to login", async ({ page }) => {
    test.setTimeout(180_000)
    await signIn(page)

    // An expired token is the everyday 401. The shell must not keep showing a
    // half-authenticated page: RunTimeline clears the session and replaces the
    // route (RunTimeline.tsx:58-61). Only the stream is injected — the run itself
    // is real, because `ApplicationRunPage` renders an `ErrorNotice` instead of
    // `RunTimeline` when the aggregate read 404s, so a synthetic id would never
    // issue the stream request this spec is about.
    const runId = await startApplicationRun(page)
    await page.route(`**/api/v1/application-runs/${runId}/events`, (route) =>
      failWith(route, 401, "UNAUTHORIZED", "登录已过期", "req-e2e-401"),
    )
    await page.reload()
    await expect(page).toHaveURL(/\/login$/)
  })
})

test.describe("FIN-011 SSE resilience", () => {
  test("INJECTED: a gap in the sequence forces a reconnect carrying Last-Event-ID", async ({ page }) => {
    test.setTimeout(180_000)
    await signIn(page)

    // A real run with an injected stream. The page mounts `RunTimeline` only after
    // the aggregate read succeeds, so a synthetic id would stop at an ErrorNotice
    // and never open the stream this spec is about.
    const runId = await startApplicationRun(page)
    const seenLastEventId: (string | null)[] = []

    // Branch on the cursor, never on a call counter. `RunTimeline`'s effect runs
    // twice under `<StrictMode>` in dev — and the e2e stack *is* `vite dev` — so,
    // of the first two requests, the live connection is the second one and the
    // first is the discarded mount. A counter therefore hands the "fresh
    // subscribe" frames to a dead connection and answers the live one with the
    // "resumed" frames, which the client reads as a gap (`lastAccepted` is still
    // -1) and answers by reconnecting forever. `Last-Event-ID` describes the
    // protocol instead of the mount order: absent means a fresh subscribe,
    // present means a resume from that sequence.
    await page.route(`**/api/v1/application-runs/${runId}/events`, async (route) => {
      // `headers()` lowercases names; `allHeaders()` is the complete set but is
      // async, so await it inside the handler rather than reading a snapshot.
      const headers = await route.request().allHeaders()
      const cursor = headers["last-event-id"] ?? null
      seenLastEventId.push(cursor)
      if (cursor === null) {
        // Deliver 0,1 then jump to 5. The client must refuse to apply 5 on top
        // of 1 and reconnect from 1 rather than render a hole in the timeline
        // (`classifySequence` -> "gap" -> abandon this stream, resume the next).
        await streamFrames(route, [
          sseHeartbeat(),
          sseEvent({ sequence: 0, eventType: "RUN_CREATED", status: "CREATED", runId }),
          sseEvent({ sequence: 1, eventType: "NODE_COMPLETED", status: "RUNNING", runId, node: "load" }),
          sseEvent({ sequence: 5, eventType: "NODE_COMPLETED", status: "RUNNING", runId, node: "skipped" }),
        ])
        return
      }
      // Replay from the cursor: 2,3,4 then a terminal frame, so the stream can
      // converge and close cleanly.
      await streamFrames(route, [
        sseEvent({ sequence: 2, eventType: "NODE_COMPLETED", status: "RUNNING", runId, node: "b" }),
        sseEvent({ sequence: 3, eventType: "NODE_COMPLETED", status: "RUNNING", runId, node: "c" }),
        sseEvent({ sequence: 4, eventType: "NODE_COMPLETED", status: "RUNNING", runId, node: "d" }),
        sseEvent({ sequence: 5, eventType: "RUN_COMPLETED", status: "COMPLETED", runId }),
      ])
    })

    // Reload so the client opens a *fresh* stream with no cursor, instead of
    // reusing the live one it already holds from the setup run.
    await page.reload()
    const timeline = page.getByRole("region", { name: "执行时间线" })

    // The gap is reported to the user, and the reconnect replays instead of
    // dropping the timeline (the events before the gap must survive).
    await expect(timeline).toContainText("#0")
    const resumed = seenLastEventId.filter((value) => value !== null)
    await expect.poll(() => resumed.length, { timeout: 20_000 }).toBeGreaterThanOrEqual(1)
    // The resume carries the last *accepted* sequence: 1. Not 0 (the heartbeat
    // never advances the cursor) and not 5 (the gap was refused, not applied).
    // Exactly one resume is expected, because the replayed stream ends terminal.
    expect(resumed).toEqual(["1"])
    await expect(timeline).toContainText("流程完成")
    // The skipped frame is never rendered as if it were contiguous.
    await expect(timeline.getByText("skipped", { exact: true })).toHaveCount(0)
  })

  test("INJECTED: a lost Redis notification is recovered by the heartbeat, not by a manual refresh", async ({ page }) => {
    test.setTimeout(180_000)
    await signIn(page)

    // Real run, injected stream: `RunTimeline` is only mounted once the aggregate
    // read succeeds, so a synthetic id would never open a stream to recover.
    const runId = await startApplicationRun(page)

    // Keyed on the cursor rather than a call counter, for the same reason as the
    // gap spec above: `<StrictMode>` makes the first subscribe belong to the
    // discarded mount, so a counter answers the live connection with the resume
    // frames — and a lone sequence 1 against `lastAccepted = -1` is a gap, which
    // the client answers by reconnecting forever.
    await page.route(`**/api/v1/application-runs/${runId}/events`, async (route) => {
      const cursor = (await route.request().allHeaders())["last-event-id"] ?? null
      if (cursor === null) {
        // The worker committed but the pub/sub wake-up never arrived. The
        // stream must not depend on the notification: the heartbeat re-reads
        // PostgreSQL (sse/service.py, `test_heartbeat_discovers_events_without_notify`).
        // A heartbeat frame is what separates the two: nothing but the deadline can
        // deliver the terminal event that follows it.
        await streamFrames(route, [
          sseEvent({ sequence: 0, eventType: "RUN_CREATED", status: "CREATED", runId }),
          sseHeartbeat(),
          sseEvent({ sequence: 1, eventType: "RUN_COMPLETED", status: "COMPLETED", runId }),
        ])
        return
      }
      await streamFrames(route, [sseEvent({ sequence: 1, eventType: "RUN_COMPLETED", status: "COMPLETED", runId })])
    })

    // Reload so the client opens a fresh stream against the injected handler.
    await page.reload()
    const timeline = page.getByRole("region", { name: "执行时间线" })
    // Discovered by polling rather than a notification, and closed on terminal.
    await expect(timeline).toContainText("流程完成")
    await expect(page.getByText("已结束", { exact: true })).toBeVisible()
  })

  test("INJECTED: a terminal frame closes the stream and stops reconnecting", async ({ page }) => {
    test.setTimeout(180_000)
    await signIn(page)

    const runId = await startApplicationRun(page)
    let calls = 0

    await page.route(`**/api/v1/application-runs/${runId}/events`, async (route) => {
      calls += 1
      await streamFrames(route, [
        sseEvent({ sequence: 0, eventType: "RUN_CREATED", status: "CREATED", runId }),
        sseEvent({ sequence: 1, eventType: "RUN_FAILED", status: "FAILED", runId }),
      ])
    })

    // A real run with an injected stream. `ApplicationRunPage` renders an
    // `ErrorNotice` instead of `RunTimeline` when the aggregate read fails
    // (ApplicationRunPage.tsx:32), so a synthetic id would never mount the only
    // component that opens an event stream: the status below could never appear
    // and the call-count assertion would be vacuous. Reload so the stream under
    // test is the injected one, not the live one the setup run already opened.
    await page.reload()
    await expect(page.getByText("已结束", { exact: true })).toBeVisible()
    // A terminal status is final: the client must not reopen the stream forever.
    const callsAtClose = calls
    await page.waitForTimeout(3_000)
    expect(calls).toBe(callsAtClose)
  })

  test("a real JobAssignment revocation stops the live stream", async ({ page, browser }) => {
    test.setTimeout(240_000)
    await signIn(page)

    // Real path (item 3): an assigned HIRING_MANAGER holds the stream and the HR
    // withdraws that manager's assignment, so the *server* closes the stream with
    // SSE_AUTH_REVOKED. Nothing is injected — the refusal comes from
    // `authorize_job` on the next heartbeat (`sse/service.py`).
    //
    // Two identities are required, and not as a convenience:
    //   * a JobAssignment only constrains HIRING_MANAGER — `JobService.get_authorized`
    //     returns any job to an HR actor outright, so revoking an HR's own
    //     assignment withdraws nothing and the stream never notices;
    //   * only an HR may call the assignment API (`revoke_assignment` requires the
    //     role) and only an HR may start a run (`POST /jobs/{id}/match-runs` is
    //     HR-only), so the manager cannot drive the setup either.
    // Hence: the HR (the `page` fixture) builds the run, the manager watches it
    // in its own context, and the HR revokes the manager.
    const runId = await startApplicationRun(page)

    const hrSession = await readSession(page)
    const hrToken = hrSession.token
    expect(hrToken, "the HR session must expose an access token for the API calls").toBeTruthy()
    const api = await resolveDemoJobApi(page, hrToken!)

    const managerContext = await browser.newContext({ baseURL: new URL(page.url()).origin })
    const managerPage = await managerContext.newPage()
    let managerUserId: string | undefined
    try {
      await signInAs(managerPage, DEMO_MANAGER_USERNAME)
      managerUserId = (await readSession(managerPage)).user?.id
      expect(managerUserId, "the manager session must expose its user id").toBeTruthy()

      // The manager subscribes to the run the HR just created. Reading the run
      // needs the assignment, which the seed grants, so the stream is live.
      await managerPage.goto(`/application-runs/${runId}`)
      await expect(managerPage.getByText("实时同步", { exact: true })).toBeVisible({
        timeout: 30_000,
      })
      // ...and the stream actually renders. This is not decoration: the run is
      // parked at the approval gate with ten events, so a timeline that shows
      // none of them means the frames arrived and were thrown away. That is what
      // the phantom-gap bug did — the client called the first frame of a fresh
      // subscription (sequence 1, since `agent_events.sequence` is 1-based) a
      // "gap", abandoned the stream before accepting anything, and reconnected
      // without a cursor forever (sse.test.ts, `classifySequence(1, -1)`).
      await expect(managerPage.getByRole("region", { name: "执行时间线" })).toContainText("#1")

      // ...and the assignment is withdrawn underneath the open stream.
      const revoked = await api.revoke(hrToken!, managerUserId!)
      // 204 = revoked now; 409 ASSIGNMENT_NOT_ACTIVE = already revoked, which is
      // the same precondition for what this test asserts.
      expect([204, 409]).toContain(revoked.status)

      // The server withholds further business events and closes with the
      // revocation frame; RunTimeline surfaces "授权已撤销" and ends the stream.
      await expect(managerPage.getByText("授权已撤销", { exact: true })).toBeVisible({
        timeout: 60_000,
      })
      await expect(managerPage.getByText("已结束", { exact: true })).toBeVisible()
    } finally {
      // Restore access: the seeded demo job is shared by every other spec, so
      // leaving the assignment revoked would cascade.
      if (managerUserId) await api.grant(hrToken!, managerUserId)
      await managerContext.close()
    }
  })

  test("INJECTED: mid-stream revocation closes the stream and withholds later events", async ({ page }) => {
    test.setTimeout(180_000)
    await signIn(page)

    // A real run, for the reason spelled out in the terminal-frame spec: the
    // aggregate read must succeed or `RunTimeline` is never mounted, and a
    // synthetic id would leave this assertion testing a page that shows an
    // `ErrorNotice`.
    const runId = await startApplicationRun(page)
    await page.route(`**/api/v1/application-runs/${runId}/events`, (route) =>
      streamFrames(route, [
        sseEvent({ sequence: 0, eventType: "RUN_CREATED", status: "CREATED", runId }),
        sseEvent({ sequence: 1, eventType: "NODE_COMPLETED", status: "RUNNING", runId, node: "load" }),
        sseAuthRevoked(),
        // Emitted after revocation on purpose: the server withholds business
        // events once authorization fails (test_sse.py asserts sequences [0,1]),
        // and the browser must not render this one either.
        sseEvent({ sequence: 2, eventType: "NODE_COMPLETED", status: "RUNNING", runId, node: "must-not-render" }),
      ]),
    )

    await page.reload()
    const timeline = page.getByRole("region", { name: "执行时间线" })
    await expect(page.getByText("授权已撤销", { exact: true })).toBeVisible()
    await expect(page.getByText("已结束", { exact: true })).toBeVisible()
    await expect(timeline).toContainText("#1")
    // Withheld: the frame emitted after revocation must never be rendered. Give
    // the stream a moment to (incorrectly) deliver it before asserting absence,
    // so this cannot pass merely because the assertion ran too early.
    await page.waitForTimeout(2_000)
    await expect(timeline.getByText("#2", { exact: true })).toHaveCount(0)
    await expect(timeline.getByText("must-not-render")).toHaveCount(0)
  })
})

// --- helpers ----------------------------------------------------------------

/**
 * Read the session the SPA persists in `sessionStorage`: the bearer token for
 * direct API calls, and the user id an assignment is addressed by.
 */
async function readSession(page: Page): Promise<{ token?: string; user?: { id?: string } }> {
  return (await page.evaluate(() =>
    JSON.parse(sessionStorage.getItem("resume-copilot.session") ?? "null"),
  )) as { token?: string; user?: { id?: string } }
}

/**
 * A tiny direct-API client for the demo job's assignments.
 *
 * FIN-011 item 3 needs to revoke an assignment *while a page holds an open SSE
 * stream*, which the UI cannot do from a single browser context: the revoke
 * control lives on the job detail page, and navigating there would tear down
 * the run stream under test. Calling the API from the test process keeps the
 * stream alive and still exercises the genuine server-side authorization path.
 */
async function resolveDemoJobApi(page: Page, token: string) {
  const base = new URL(page.url()).origin
  const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }

  const jobs = (await (await fetch(`${base}/api/v1/jobs`, { headers })).json()) as {
    items: Array<{ id: string; title: string }>
  }
  const job = jobs.items.find((item) => item.title === DEMO_JOB)
  expect(job, `the seeded demo job must exist: ${DEMO_JOB}`).toBeTruthy()

  return {
    /** DELETE /jobs/{id}/assignments/{userId} -> 204 (jobs/routes.py). */
    async revoke(accessToken: string, userId: string): Promise<{ status: number }> {
      const response = await fetch(`${base}/api/v1/jobs/${job!.id}/assignments/${userId}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${accessToken}` },
      })
      return { status: response.status }
    },
    /** Re-grant the assignment so the shared seed is left as it was found. */
    async grant(accessToken: string, userId: string): Promise<void> {
      const response = await fetch(`${base}/api/v1/jobs/${job!.id}/assignments`, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId }),
      })
      // 409 ASSIGNMENT_EXISTS is fine: it means the assignment was already
      // active (the revoke never took effect), so the seed is still intact.
      expect(
        response.ok || response.status === 409,
        `the assignment must end up active again, got ${response.status}`,
      ).toBe(true)
    },
  }
}

/**
 * Drive the demo job to a single application run and return with the run page
 * open, waiting for the first approval gate to appear.
 *
 * This reuses the seeded job rather than uploading a resume: FIN-011 is about
 * failure handling, and FIN-010 already owns the "brand-new candidate" path. A
 * fresh run per test is still needed, because approving is destructive to the
 * approval under test, so each test takes its own run off the shared ranking.
 */
async function startApplicationRun(page: Page): Promise<string> {
  await openJob(page)
  await page.getByRole("button", { name: "启动批量分析" }).click()
  await expect(page).toHaveURL(/\/match-runs\/[0-9a-f-]+$/)

  const ranking = page.getByRole("region", { name: "候选人排名" })
  await pollFor(page, "a ranking row to offer a single-candidate run", async () => {
    return (await ranking.getByRole("button", { name: "启动单人流程" }).count()) > 0
  })
  await ranking.getByRole("button", { name: "启动单人流程" }).first().click()
  await expect(page).toHaveURL(/\/application-runs\/[0-9a-f-]+$/)
  // The id is returned for the specs that inject the *stream* against a run that
  // really exists. `ApplicationRunPage` short-circuits to an `ErrorNotice` when the
  // aggregate read fails (`ApplicationRunPage.tsx:31-34`), so `RunTimeline` — the
  // only component that opens an event stream — is never mounted for a synthetic id;
  // a route faking that stream would then never be exercised at all.
  const runId = page.url().split("/application-runs/")[1] ?? ""
  expect(runId, "the application run page must expose its run id").toMatch(/^[0-9a-f-]{36}$/)
  await expect(
    page.locator(".detail-meta").getByText("等待审批", { exact: true }),
  ).toBeVisible({ timeout: 30_000 })
  return runId
}

async function openCurrentApproval(page: Page): Promise<void> {
  await page.getByRole("link", { name: "查看并决策 →" }).click()
  await expect(page.getByRole("heading", { name: "审批决策" })).toBeVisible()
}

async function decideOnCurrentApproval(page: Page, action: "通过" | "驳回"): Promise<void> {
  await openCurrentApproval(page)
  await page.getByRole("button", { name: action }).click()
}
