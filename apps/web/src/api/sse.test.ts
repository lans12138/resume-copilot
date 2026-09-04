import { describe, expect, it } from "vitest"
import { classifySequence, parseSseBlocks } from "./sse"

const event = (seq: number, status = "RUNNING"): string =>
  `event: STATUS_CHANGED\ndata: ${JSON.stringify({ event_id: `e${seq}`, run_id: "r", run_type: "MATCH", sequence: seq, event_type: "STATUS_CHANGED", node: null, status, message_key: "x", safe_payload: {}, occurred_at: null })}\n\n`

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
