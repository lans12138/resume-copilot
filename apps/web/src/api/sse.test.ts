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

