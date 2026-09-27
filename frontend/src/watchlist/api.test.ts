import { afterEach, expect, it, vi } from "vitest"
import { addWatch, getWatchlist } from "./api"

afterEach(() => vi.unstubAllGlobals())

it("uses the selected physical model on watch reads and creation", async () => {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ items: [] })))
  vi.stubGlobal("fetch", fetchMock)
  await getWatchlist("gjr_garch_t")
  await addWatch("opaque-key", "gjr_garch_t")
  expect(fetchMock.mock.calls[0][0]).toBe("/api/watchlist?forecast_model=gjr_garch_t")
  expect(fetchMock.mock.calls[1][0]).toBe("/api/watchlist?forecast_model=gjr_garch_t")
  expect(fetchMock.mock.calls[1][1]).toEqual(expect.objectContaining({ method: "POST", body: JSON.stringify({ watch_key: "opaque-key" }) }))
})
