import { describe, expect, it, vi } from "vitest"
import { classifySequence, connectRunEvents, parseSseBlocks } from "./sse"

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
})

describe("connectRunEvents self-heal", () => {
  it("reconnects and converges on terminal when the stream ends without one", async () => {
    let calls = 0
    const fetchMock = vi.fn(async (_url: string, _init: unknown) => {
      calls += 1
      if (calls === 1) {
        // First connection delivers RUN_CREATED then the proxy drops the stream.
        return new Response(streamOf(["retry: 1000\n\n", event(0, "CREATED")]), {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        })
      }
      // Reconnect replays and finds the run already COMPLETED.
      return new Response(streamOf([event(1, "COMPLETED")]), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      })
    })
    const onEvent = vi.fn()
    const onClosed = vi.fn()

    const conn = connectRunEvents("r", {
      runType: "MATCH",
      token: "t",
      fetchImpl: fetchMock as unknown as typeof fetch,
      onEvent,
      onClosed,
      maxBackoffMs: 30,
    })

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2), { timeout: 2000 })
    await vi.waitFor(
      () => expect(onClosed).toHaveBeenCalledWith("terminal"),
      { timeout: 2000 },
    )
    expect(onEvent).toHaveBeenCalledWith(
      expect.objectContaining({ sequence: 1, status: "COMPLETED" }),
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
  /** Capture the headers of each request so the resume cursor can be asserted. */
  function recordingFetch(responses: Array<() => Response>) {
    const seen: Array<string | null> = []
    let call = 0
    const impl = vi.fn(async (_url: string, init: { headers?: Record<string, string> }) => {
      seen.push(init?.headers?.["Last-Event-ID"] ?? null)
      const make = responses[Math.min(call, responses.length - 1)]
      call += 1
      return make()
    })
    return { impl, seen }
  }

  it("treats a skipped sequence as a gap and resumes from the last accepted id", async () => {
    const { impl, seen } = recordingFetch([
      // Deliver 0,1 then jump to 5: the client must not accept 5 on top of 1.
      () =>
        new Response(streamOf([event(0, "RUNNING"), event(1, "RUNNING"), event(5, "RUNNING")]), {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        }),
      () => new Response(streamOf([event(2, "RUNNING"), event(3, "COMPLETED")]), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    ])
    const onEvent = vi.fn()
    const onError = vi.fn()

    const conn = connectRunEvents("r", {
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
    // The replayed frame 5 is NOT delivered as a continuation of 1.
    expect(onEvent.mock.calls.map(([e]) => e.sequence)).not.toContain(5)
    // The reconnect resumes from the last accepted sequence rather than the
    // start: without this the replay would re-render everything from 0.
    await vi.waitFor(() => expect(seen.length).toBeGreaterThanOrEqual(2), { timeout: 2000 })
    expect(seen[1]).toBe("1")
    conn.close()
  })

  it("resumes without a Last-Event-ID on first connect and sends it after a drop", async () => {
    const { impl, seen } = recordingFetch([
      () => new Response(streamOf([event(0), event(1)]), { status: 200, headers: { "content-type": "text/event-stream" } }),
      () => new Response(streamOf([event(2, "COMPLETED")]), { status: 200, headers: { "content-type": "text/event-stream" } }),
    ])

    const conn = connectRunEvents("r", {
      runType: "MATCH",
      token: "t",
      fetchImpl: impl as unknown as typeof fetch,
      onEvent: vi.fn(),
      maxBackoffMs: 30,
    })

    await vi.waitFor(() => expect(seen.length).toBeGreaterThanOrEqual(2), { timeout: 2000 })
    // First attempt carries no cursor; the resume asks for exactly the next one.
    expect(seen[0]).toBeNull()
    expect(seen[1]).toBe("1")
    conn.close()
  })

  it("stops on a revoked stream and withholds the frames that follow it", async () => {
    let calls = 0
    const impl = vi.fn(async () => {
      calls += 1
      return new Response(
        streamOf([
          event(0),
          "event: SSE_AUTH_REVOKED\ndata: {}\n\n",
          // Emitted after revocation on purpose: the server withholds business
          // events once authorization fails, and the browser must too.
          event(1),
        ]),
        { status: 200, headers: { "content-type": "text/event-stream" } },
      )
    })
    const onEvent = vi.fn()
    const onError = vi.fn()
    const onClosed = vi.fn()

    const conn = connectRunEvents("r", {
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
    expect(onEvent.mock.calls.map(([e]) => e.sequence)).toEqual([0])
    expect(onClosed).toHaveBeenCalledWith("client")
    // Revocation is final for this session: it must not reconnect in a loop.
    const callsAfterStop = calls
    await new Promise((resolve) => setTimeout(resolve, 120))
    expect(calls).toBe(callsAfterStop)
    conn.close()
  })

  it("stops reconnecting once a terminal status has been delivered", async () => {
    let calls = 0
    const impl = vi.fn(async () => {
      calls += 1
      return new Response(streamOf([event(calls - 1, "COMPLETED")]), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      })
    })
    const onClosed = vi.fn()

    const conn = connectRunEvents("r", {
      runType: "MATCH",
      token: "t",
      fetchImpl: impl as unknown as typeof fetch,
      onEvent: vi.fn(),
      onClosed,
      maxBackoffMs: 20,
    })

    await vi.waitFor(() => expect(onClosed).toHaveBeenCalledWith("terminal"), { timeout: 2000 })
    const callsAtClose = calls
    await new Promise((resolve) => setTimeout(resolve, 150))
    expect(calls).toBe(callsAtClose)
    conn.close()
  })

  it("does not retry a 401 and stops the session", async () => {
    const impl = vi.fn(async () => new Response("{}", { status: 401, headers: { "content-type": "application/json" } }))
    const onError = vi.fn()
    const onClosed = vi.fn()

    const conn = connectRunEvents("r", {
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
})

