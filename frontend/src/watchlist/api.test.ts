import { afterEach, expect, it, vi } from "vitest"
import { addWatch, deleteWatch, getJob, getWatchlist, refreshWatchlist } from "./api"

const watch = {
  id: "watch-1", ticker: "IREN", root: "IREN", side: "call", expiration: "2026-10-16",
  strike_exact: "50.000", terms_note: "Assuming standard 100-share terms.",
  created_at: "2026-09-11T14:00:00Z", outcome: { status: "pending" },
}

afterEach(() => vi.unstubAllGlobals())

it("uses the selected physical model on watch reads and creation", async () => {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => new Response(JSON.stringify(
    init?.method === "POST" ? { item: watch, created: true, job: null } : { items: [] },
  )))
  vi.stubGlobal("fetch", fetchMock)
  await getWatchlist("gjr_garch_t")
  await addWatch("opaque-key", "gjr_garch_t")
  expect(fetchMock.mock.calls[0][0]).toBe("/api/watchlist?forecast_model=gjr_garch_t")
  expect(fetchMock.mock.calls[1][0]).toBe("/api/watchlist?forecast_model=gjr_garch_t")
  expect(fetchMock.mock.calls[1][1]).toEqual(expect.objectContaining({ method: "POST", body: JSON.stringify({ watch_key: "opaque-key" }) }))
  expect(fetchMock.mock.calls[1][1]!.headers).toEqual({ "Content-Type": "application/json" })
})

it.each([
  ["missing items", () => getWatchlist(), {}],
  ["null items", () => getWatchlist(), { items: null }],
  ["fractional financial field", () => getWatchlist(), { items: [{ ...watch, hypothetical_risk: { assumed_bid_cents: 12.5 } }] }],
  ["unknown job state", () => getJob("job/1"), { id: "job-1", kind: "watch_refresh", state: "done" }],
  ["missing creation fields", () => addWatch("opaque-key"), { items: [] }],
  ["missing refresh job", () => refreshWatchlist(), {}],
  ["invalid refresh job", () => refreshWatchlist(), { job: { state: "queued" } }],
] as const)("rejects successful %s", async (_label, request, body) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(body))))
  await expect(request()).rejects.toThrow("Local API returned an invalid response")
})

it("validates job defaults", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "job-1", kind: "watch_refresh", state: "queued" }))))
  await expect(getJob("job/1")).resolves.toMatchObject({ progress: 0, message: "" })
})

it("accepts valid creation and refresh payloads and deletes without parsing a body", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ item: watch, created: true, job: null })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ job: null })))
    .mockResolvedValueOnce(new Response(null, { status: 204 }))
  vi.stubGlobal("fetch", fetchMock)
  await expect(addWatch("opaque-key")).resolves.toMatchObject({ created: true, job: null })
  await expect(refreshWatchlist()).resolves.toEqual({ job: null })
  await expect(deleteWatch("watch/1")).resolves.toBeUndefined()
  expect(fetchMock.mock.calls[2][0]).toBe("/api/watchlist/watch%2F1")
  expect(fetchMock.mock.calls[2][1]).toMatchObject({ method: "DELETE", signal: expect.any(AbortSignal) })
})

it("normalizes invalid successful JSON", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("not-json")))
  await expect(getWatchlist()).rejects.toThrow("Local API returned an invalid response")
})
