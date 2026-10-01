import { afterEach, expect, it, vi } from "vitest"
import { boundedFetch } from "./boundedFetch"

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

it("preserves request options and combines caller cancellation with the native deadline", async () => {
  const caller = new AbortController()
  const deadline = new AbortController()
  const timeout = vi.spyOn(AbortSignal, "timeout").mockReturnValue(deadline.signal)
  const fetchMock = vi.fn().mockResolvedValue(new Response("{}"))
  vi.stubGlobal("fetch", fetchMock)
  const headers = { "Content-Type": "application/json", "X-Test": "preserved" }
  await boundedFetch("/api/watchlist", {
    method: "POST", headers, body: '{"watch_key":"opaque"}', cache: "no-store", signal: caller.signal,
  })
  expect(timeout).toHaveBeenCalledWith(60_000)
  expect(fetchMock.mock.calls[0][1]).toMatchObject({
    method: "POST", headers, body: '{"watch_key":"opaque"}', cache: "no-store",
  })
  const combined = fetchMock.mock.calls[0][1].signal as AbortSignal
  expect(combined.aborted).toBe(false)
  caller.abort()
  expect(combined.aborted).toBe(true)
  expect(combined.reason).toBe(caller.signal.reason)
})

it("honors an already aborted selection and the requested deadline", async () => {
  const controller = new AbortController()
  controller.abort()
  const timeout = vi.spyOn(AbortSignal, "timeout")
  const fetchMock = vi.fn().mockResolvedValue(new Response("{}"))
  vi.stubGlobal("fetch", fetchMock)
  await boundedFetch("/api/tickers", { signal: controller.signal }, 30_000)
  expect(timeout).toHaveBeenCalledWith(30_000)
  expect(fetchMock.mock.calls[0][1].signal.reason).toBe(controller.signal.reason)
})
