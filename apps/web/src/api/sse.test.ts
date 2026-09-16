import { afterEach, describe, expect, it, vi } from "vitest"
import {
  classifySequence,
  connectRunEvents,
  parseSseBlocks,
  type ConnectOptions,
} from "./sse"

/**
 * One `event:`/`data:` frame carrying a given sequence.
 *
 * Sequences here are 1-based, matching the server: `AgentEvent.sequence` is
 * allocated from 1 (detailed design §4.5). Fixtures that started at 0 modelled a
 * numbering the server never emits, which is how a client that called the first
 * real frame a "gap" stayed green in this file while every live stream looped.
 */
const event = (seq: number, status = "RUNNING"): string =>
  `event: STATUS_CHANGED\ndata: ${JSON.stringify({ event_id: `e${seq}`, run_id: "r", run_type: "MATCH", sequence: seq, event_type: "STATUS_CHANGED", node: null, status, message_key: "x", safe_payload: {}, occurred_at: null })}\n\n`

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  let i = 0
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(new TextEncoder().encode(chunks[i++]))
      } else {
        controller.close()
      }
    },
  })
}

/**
 * Every connection opened here is closed after its test, pass or fail.
 *
 * `conn.close()` written as the last line of a test body is skipped the moment an
 * assertion throws, and a client that is mid-reconnect keeps opening streams
 * after its test has gone. That is not hypothetical: a regression that made the
 * first frame of a run look like a sequence gap reconnected immediately and
 * without backoff, so the leaked connections allocated streams until the vitest
 * worker died of heap exhaustion. One wrong assumption then reads as "the whole
 * file crashed" rather than "this assertion failed".
 */
const openConnections: Array<{ close: () => void }> = []

function connect(runId: string, opts: ConnectOptions) {
  const connection = connectRunEvents(runId, opts)
  openConnections.push(connection)
  return connection
}

afterEach(() => {
  while (openConnections.length > 0) openConnections.pop()!.close()
})

/**
 * A `fetch` stand-in that replays prepared responses and records the resume
 * cursor of every request.
 *
 * It answers a bounded number of attempts and then refuses with a 404, which the
 * client does not retry. That bound is not decoration. A gap reconnect is
 * *immediate* (`reconnectFrom`) and each iteration completes in a microtask, so a
 * client that misclassifies a sequence spins a pure microtask loop: it starves
 * timers, which means neither `vi.waitFor`'s timeout nor the `setTimeout`-based
 * backoff can ever fire, and the worker dies of heap exhaustion. One wrong
 * assumption would then read as "the whole file crashed" rather than "this
 * assertion failed". The cap is the only bound that does not depend on a timer.
 */
function recordingFetch(responses: Array<() => Response>) {
  const seen: Array<string | null> = []
  let call = 0
  const impl = vi.fn(async (_url: string, init: { headers?: Record<string, string> }) => {
    seen.push(init?.headers?.["Last-Event-ID"] ?? null)
    if (call > responses.length + 3) {
      call += 1
      return new Response("{}", { status: 404, headers: { "content-type": "application/json" } })
    }
    const make = responses[Math.min(call, responses.length - 1)]
    call += 1
    return make()
  })
  return { impl, seen }
}

describe("parseSseBlocks", () => {
  it("parses a complete event frame into an envelope", () => {
    const { blocks, rest } = parseSseBlocks(event(3))
    expect(blocks).toHaveLength(1)
    expect(blocks[0].kind).toBe("event")
    expect(blocks[0].event?.sequence).toBe(3)
    expect(rest).toBe("")
  })

  it("treats a leading colon line as a heartbeat", () => {
    const { blocks } = parseSseBlocks(": keep-alive\n\n")
    expect(blocks).toHaveLength(1)
    expect(blocks[0].kind).toBe("heartbeat")
  })

  it("emits an auth_revoked block for SSE_AUTH_REVOKED", () => {
    const { blocks } = parseSseBlocks("event: SSE_AUTH_REVOKED\ndata: {}\n\n")
    expect(blocks[0].kind).toBe("auth_revoked")
  })

  it("returns an incomplete trailing segment as rest", () => {
    const partial = event(1).slice(0, event(1).length - 4)
    const { blocks, rest } = parseSseBlocks(partial)
    expect(blocks).toHaveLength(0)
    expect(rest.length).toBeGreaterThan(0)
  })

  it("drops malformed JSON data silently", () => {
    const { blocks } = parseSseBlocks("event: STATUS_CHANGED\ndata: not-json\n\n")
    expect(blocks).toHaveLength(0)
  })
})

describe("classifySequence", () => {
  it("marks <= lastAccepted as duplicate", () => {
    expect(classifySequence(5, 5)).toBe("duplicate")
    expect(classifySequence(4, 5)).toBe("duplicate")
  })
  it("marks next sequence as accept", () => {
    expect(classifySequence(6, 5)).toBe("accept")
  })
  it("marks a skipped sequence as gap", () => {
    expect(classifySequence(8, 5)).toBe("gap")
  })
  it("accepts the first frame of a fresh subscription, which is sequence 1", () => {
    // `AgentEvent.sequence` is assigned from 1 (detailed design §4.5,
    // `next_event_sequence`: 从 1 开始分配), so the first frame of a subscription
    // with no cursor is sequence 1 — never `0 + 1`. Reading that as a gap made
    // every fresh stream abandon itself before accepting an event, reconnect
    // without a cursor, and replay the same frame forever.
    expect(classifySequence(1, -1)).toBe("accept")
    // Nothing to be continuous with yet, so no gap can be claimed at all.
    expect(classifySequence(5, -1)).toBe("accept")
  })
})

describe("connectRunEvents self-heal", () => {
  it("reconnects and converges on terminal when the stream ends without one", async () => {
    const { impl, seen } = recordingFetch([
      // First connection delivers RUN_CREATED then the proxy drops the stream.
      () =>
        new Response(streamOf(["retry: 1000\n\n", event(1, "CREATED")]), {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        }),
      // Reconnect replays and finds the run already COMPLETED.
      () =>
        new Response(streamOf([event(2, "COMPLETED")]), {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        }),
    ])
    const onEvent = vi.fn()
    const onClosed = vi.fn()

    const conn = connect("r", {
      runType: "MATCH",
      token: "t",
      fetchImpl: impl as unknown as typeof fetch,
      onEvent,
      onClosed,
      maxBackoffMs: 30,
    })

    await vi.waitFor(() => expect(seen.length).toBe(2), { timeout: 2000 })
    await vi.waitFor(
      () => expect(onClosed).toHaveBeenCalledWith("terminal"),
      { timeout: 2000 },
    )
    expect(onEvent).toHaveBeenCalledWith(
      expect.objectContaining({ sequence: 2, status: "COMPLETED" }),
    )
    conn.close()
  })
})

/**
 * FIN-011: the browser-level half of the SSE safety contract.
 *
 * These complement `fin011-failure-matrix.spec.ts`. They run without Docker, so
 * the *client* invariants (cursor arithmetic, revocation withholding, terminal
 * finality) stay enforced even where the browser suite cannot execute.
 */
describe("connectRunEvents failure handling (FIN-011)", () => {
  it("treats a skipped sequence as a gap and resumes from the last accepted id", async () => {
    const { impl, seen } = recordingFetch([
      // Deliver 1,2 then jump to 5: the client must not accept 5 on top of 2.
      () =>
        new Response(streamOf([event(1, "RUNNING"), event(2, "RUNNING"), event(5, "RUNNING")]), {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        }),
      () => new Response(streamOf([event(3, "RUNNING"), event(4, "COMPLETED")]), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    ])
    const onEvent = vi.fn()
    const onError = vi.fn()

    const conn = connect("r", {
      runType: "MATCH",
      token: "t",
      fetchImpl: impl as unknown as typeof fetch,
      onEvent,
      onError,
      maxBackoffMs: 30,
    })

    await vi.waitFor(() => expect(impl).toHaveBeenCalledTimes(2), { timeout: 2000 })
    // The gap is reported, never silently stitched over.
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ kind: "gap" }))
    // The replayed frame 5 is NOT delivered as a continuation of 2.
    expect(onEvent.mock.calls.map(([e]) => e.sequence)).not.toContain(5)
    // The reconnect resumes from the last accepted sequence rather than the
    // start: without this the replay would re-render everything from 1.
    await vi.waitFor(() => expect(seen.length).toBeGreaterThanOrEqual(2), { timeout: 2000 })
    expect(seen[1]).toBe("2")
    conn.close()
  })

  it("starts a fresh subscription at sequence 1 instead of looping on a phantom gap", async () => {
    // Regression guard for the bug that made every live timeline render zero
    // rows: the server numbers events from 1, and `lastAccepted` starts at -1
    // ("no cursor"), so the first frame is 1, not 0. The old arithmetic
    // (`sequence > lastAccepted + 1`) called 1 a gap, abandoned the stream,
    // reconnected without a cursor, and replayed the same frame forever.
    //
    // `recordingFetch` bounds a regression (see its docstring): the handler stops
    // answering after a few attempts with a 404, which the client does not retry.
    const { impl, seen } = recordingFetch([
      () =>
        new Response(streamOf([event(1, "RUNNING"), event(2, "COMPLETED")]), {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        }),
    ])
    const onEvent = vi.fn()
    const onError = vi.fn()
    const onClosed = vi.fn()

    const conn = connect("r", {
      runType: "APPLICATION",
      token: "t",
      fetchImpl: impl as unknown as typeof fetch,
      onEvent,
      onError,
      onClosed,
      maxBackoffMs: 20,
    })

    await vi.waitFor(() => expect(onClosed).toHaveBeenCalledWith("terminal"), { timeout: 2000 })
    expect(onEvent.mock.calls.map(([e]) => e.sequence)).toEqual([1, 2])
    expect(onError).not.toHaveBeenCalled()
    await new Promise((resolve) => setTimeout(resolve, 150))
    // One connection, no cursor: the terminal frame closed it and nothing
    // reopened it.
    expect(seen).toEqual([null])
    conn.close()
  })

  it("resumes without a Last-Event-ID on first connect and sends it after a drop", async () => {
    const { impl, seen } = recordingFetch([
      () => new Response(streamOf([event(1), event(2)]), { status: 200, headers: { "content-type": "text/event-stream" } }),
      () => new Response(streamOf([event(3, "COMPLETED")]), { status: 200, headers: { "content-type": "text/event-stream" } }),
    ])

    const conn = connect("r", {
      runType: "MATCH",
      token: "t",
      fetchImpl: impl as unknown as typeof fetch,
      onEvent: vi.fn(),
      maxBackoffMs: 30,
    })

    await vi.waitFor(() => expect(seen.length).toBeGreaterThanOrEqual(2), { timeout: 2000 })
    // First attempt carries no cursor; the resume asks for exactly the next one.
    expect(seen[0]).toBeNull()
    expect(seen[1]).toBe("2")
    conn.close()
  })

  it("stops on a revoked stream and withholds the frames that follow it", async () => {
    const { impl, seen } = recordingFetch([
      () =>
        new Response(
          streamOf([
            event(1),
            "event: SSE_AUTH_REVOKED\ndata: {}\n\n",
            // Emitted after revocation on purpose: the server withholds business
            // events once authorization fails, and the browser must too.
            event(2),
          ]),
          { status: 200, headers: { "content-type": "text/event-stream" } },
        ),
    ])
    const onEvent = vi.fn()
    const onError = vi.fn()
    const onClosed = vi.fn()

    const conn = connect("r", {
      runType: "MATCH",
      token: "t",
      fetchImpl: impl as unknown as typeof fetch,
      onEvent,
      onError,
      onClosed,
      maxBackoffMs: 20,
    })

    await vi.waitFor(() => expect(onError).toHaveBeenCalledWith(expect.objectContaining({ kind: "revoked" })), {
      timeout: 2000,
    })
    expect(onEvent.mock.calls.map(([e]) => e.sequence)).toEqual([1])
    expect(onClosed).toHaveBeenCalledWith("client")
    // Revocation is final for this session: it must not reconnect in a loop.
    const callsAfterStop = seen.length
    await new Promise((resolve) => setTimeout(resolve, 120))
    expect(seen.length).toBe(callsAfterStop)
    conn.close()
  })

  it("stops reconnecting once a terminal status has been delivered", async () => {
    const { impl, seen } = recordingFetch([
      () => new Response(streamOf([event(1, "COMPLETED")]), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    ])
    const onClosed = vi.fn()

    const conn = connect("r", {
      runType: "MATCH",
      token: "t",
      fetchImpl: impl as unknown as typeof fetch,
      onEvent: vi.fn(),
      onClosed,
      maxBackoffMs: 20,
    })

    await vi.waitFor(() => expect(onClosed).toHaveBeenCalledWith("terminal"), { timeout: 2000 })
    const callsAtClose = seen.length
    await new Promise((resolve) => setTimeout(resolve, 150))
    expect(seen.length).toBe(callsAtClose)
    conn.close()
  })

  it("does not retry a 401 and stops the session", async () => {
    const impl = vi.fn(async () => new Response("{}", { status: 401, headers: { "content-type": "application/json" } }))
    const onError = vi.fn()
    const onClosed = vi.fn()

    const conn = connect("r", {
      runType: "MATCH",
      token: "t",
      fetchImpl: impl as unknown as typeof fetch,
      onEvent: vi.fn(),
      onError,
      onClosed,
      maxBackoffMs: 20,
    })

    await vi.waitFor(() => expect(onError).toHaveBeenCalledWith(expect.objectContaining({ kind: "unauthorized" })), {
      timeout: 2000,
    })
    // An expired credential cannot be fixed by retrying: it must surface at once.
    expect(impl).toHaveBeenCalledTimes(1)
    expect(onClosed).toHaveBeenCalledWith("client")
    conn.close()
  })

  it("does not retry a 404, which is how the server words 'not visible to you'", async () => {
    // `JobService.get_authorized` refuses with JOB_NOT_FOUND/404 rather than 403 —
    // it hides a job's existence instead of confirming it — so "you may not see
    // this job" arrives as a 404. That is an access decision, not a transient
    // error: retrying can never succeed, and a revoked viewer would otherwise
    // become an unbounded retry storm against the API.
    const impl = vi.fn(async () => new Response("{}", { status: 404, headers: { "content-type": "application/json" } }))
    const onError = vi.fn()
    const onClosed = vi.fn()

    const conn = connect("r", {
      runType: "APPLICATION",
      token: "t",
      fetchImpl: impl as unknown as typeof fetch,
      onEvent: vi.fn(),
      onError,
      onClosed,
      maxBackoffMs: 20,
    })

    await vi.waitFor(() => expect(onError).toHaveBeenCalledWith(expect.objectContaining({ kind: "forbidden" })), {
      timeout: 2000,
    })
    // Give the backoff schedule room to fire: with maxBackoffMs=20 a retry loop
    // would have issued several more requests by now.
    await new Promise((resolve) => setTimeout(resolve, 120))
    expect(impl).toHaveBeenCalledTimes(1)
    expect(onClosed).toHaveBeenCalledWith("client")
    conn.close()
  })
})

