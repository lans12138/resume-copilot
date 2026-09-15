import { expect, type Page, type Route } from "@playwright/test"

/**
 * Shared helpers for the FIN-011 failure/safety matrix.
 *
 * The suite runs against a *live* stack through the Vite proxy, and the browser
 * calls `/api/v1/*` on its own origin. That same-origin surface is what
 * `page.route` can intercept, which is what lets these specs drive failures
 * deterministically: a real 503 needs a real broken dependency, but a browser
 * only ever sees the response, and the response is exactly what a route handler
 * can synthesize.
 *
 * The split used throughout FIN-011:
 *   - scenarios the live stack can genuinely produce are driven for real
 *     (double decision -> 409, REJECT, cancellation, SSE reconnect);
 *   - scenarios that require breaking infrastructure (Redis notification loss,
 *     a dependency 503, mid-stream revocation) are injected, and the injected
 *     frame is built from the *real* wire format so the assertion still means
 *     "the browser handles the contract the server emits".
 *
 * Anything injected is marked with `INJECTED:` in the test title so a reader can
 * always tell which assertions rest on a real server response.
 */

export const DEMO_USERNAME = "hr.demo"
export const DEMO_PASSWORD = "demo-password-123"
export const DEMO_JOB = "[DEMO] 高级后端工程师（Go / Python）"

export async function signIn(page: Page): Promise<void> {
  await page.goto("/login")
  await page.getByLabel("用户名").fill(DEMO_USERNAME)
  await page.getByLabel("密码").fill(DEMO_PASSWORD)
  await page.getByRole("button", { name: "进入工作台" }).click()
  await expect(page.getByRole("heading", { name: "岗位工作台" })).toBeVisible()
}

/** The job link's accessible name contains brackets, which are regex metachars. */
export function demoJobPattern(): RegExp {
  return new RegExp(DEMO_JOB.replace(/[[\]]/g, "\\$&"))
}

export async function openJob(page: Page): Promise<void> {
  await page.goto("/jobs")
  await page.getByRole("link", { name: demoJobPattern() }).click()
}

/**
 * One SSE block in the server's exact wire format
 * (`backend/app/sse/schemas.py::format_event`): `event:`, `id:` (the sequence,
 * which doubles as `Last-Event-ID`), one `data:` line of JSON, blank line.
 */
export function sseEvent(options: {
  sequence: number
  eventType: string
  status: string | null
  runId: string
  messageKey?: string | null
  node?: string | null
}): string {
  const payload = {
    event_id: `00000000-0000-4000-8000-${String(options.sequence).padStart(12, "0")}`,
    run_id: options.runId,
    run_type: "APPLICATION",
    sequence: options.sequence,
    event_type: options.eventType,
    node: options.node ?? null,
    status: options.status,
    message_key: options.messageKey ?? null,
    safe_payload: {},
    occurred_at: new Date().toISOString(),
  }
  return `event: ${options.eventType}\nid: ${options.sequence}\ndata: ${JSON.stringify(payload)}\n\n`
}

/** `backend/app/sse/schemas.py::format_heartbeat` — a keep-alive comment line. */
export function sseHeartbeat(): string {
  return ":\n\n"
}

/** `backend/app/sse/schemas.py::format_auth_revoked` — the mid-stream revocation frame. */
export function sseAuthRevoked(): string {
  return 'event: SSE_AUTH_REVOKED\ndata: {"reason":"access_revoked"}\n\n'
}

/**
 * Stream `frames` as `text/event-stream`, then close. A route handler that
 * returns a string is enough for the browser to read; the client uses `fetch`
 * with a reader, so a closed body is what it treats as "reconnect".
 */
export async function streamFrames(route: Route, frames: string[]): Promise<void> {
  await route.fulfill({
    status: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    },
    body: frames.join(""),
  })
}

/**
 * Fulfil with an application error in the shape the API produces
 * (`{"code": ..., "message": ..., "request_id": ...}`), so the client's
 * `ApiError` parses it exactly as it would parse a real failure.
 */
export async function failWith(
  route: Route,
  status: number,
  code: string,
  message: string,
  requestId = "req-e2e-0001",
): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify({ code, message, request_id: requestId }),
  })
}

/**
 * Poll a probe against the live stack. Worker-owned transitions are the norm
 * here (FIN-005), so nothing may assume a state change is instant.
 */
export async function pollFor(
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
