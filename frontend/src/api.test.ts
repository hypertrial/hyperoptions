import { readdirSync, readFileSync, statSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError, fetchChain, fetchCoveredCalls, fetchTickers } from "./api"
import { samplePage, samplePutPage } from "./testFixtures"
import { getWatchlist } from "./watchlist/api"

describe("chain API", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers() })

  it("requests only the selected ticker and moneyness", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(samplePage({ ticker: "CIFR" })), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }))
    vi.stubGlobal("fetch", fetchMock)

    await fetchCoveredCalls("CIFR", "itm")

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe("/api/covered-calls/CIFR?moneyness=itm")
    expect(String(fetchMock.mock.calls[0][0])).not.toContain("nasdaq.com")
  })

  it("routes puts to the cash-secured-puts endpoint", async () => {
    const { samplePutPage } = await import("./testFixtures")
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(samplePutPage()), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }))
    vi.stubGlobal("fetch", fetchMock)
    const page = await fetchChain("IREN", "put", "otm")
    expect(fetchMock.mock.calls[0][0]).toBe("/api/cash-secured-puts/IREN?moneyness=otm")
    expect(page.ticker).toBe("IREN")
    expect(page.moneyness).toBe("otm")
  })

  it("searches the ticker universe without contacting Nasdaq", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      as_of: "2026-09-11T14:00:00Z",
      total: 1,
      results: [{ symbol: "IREN", name: "Iris Energy Limited", sector: "Finance", industry: "Crypto" }],
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }))
    vi.stubGlobal("fetch", fetchMock)
    const page = await fetchTickers("IRE")
    expect(fetchMock.mock.calls[0][0]).toBe("/api/tickers?q=IRE&limit=10")
    expect(page.results[0].symbol).toBe("IREN")
  })

  it("normalizes network failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")))
    await expect(fetchCoveredCalls("IREN", "itm")).rejects.toThrow("Local API unavailable")
  })

  it("surfaces provider status text for 502, 503, and 504", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(new Response("{}", { status: 502, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response("{}", { status: 503, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response("not-json", { status: 504 })))

    await expect(fetchCoveredCalls("IREN", "itm")).rejects.toThrow("Nasdaq unavailable")
    const universe = fetchTickers("IREN")
    await expect(universe).rejects.toBeInstanceOf(ApiError)
    await expect(universe).rejects.toMatchObject({
      status: 503,
      message: "Ticker universe unavailable",
    })
    await expect(fetchCoveredCalls("IREN", "itm")).rejects.toThrow("Nasdaq timeout")
  })

  it("rejects a successful response that is not a JSON object", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("null", {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })))
    await expect(fetchCoveredCalls("IREN", "itm")).rejects.toThrow("Local API returned an invalid response")
  })

  it("rejects a successful JSON object that is not a valid page", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ ticker: "IREN" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })))
    await expect(fetchCoveredCalls("IREN", "itm")).rejects.toThrow("Local API returned an invalid response")
  })

  it("rejects a nested contract that uses floating financial fields", async () => {
    const invalid = samplePage()
    const malformed = {
      ...invalid,
      expirations: [{
        ...invalid.expirations[0],
        contracts: [{
          expiration: "2026-09-18",
          dte: 7,
          strike: 50,
        }],
      }],
    }
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(malformed), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })))
    await expect(fetchCoveredCalls("IREN", "itm")).rejects.toThrow("Local API returned an invalid response")
  })

  function fakeDeadlines() {
    vi.useFakeTimers()
    vi.spyOn(AbortSignal, "timeout").mockImplementation((milliseconds) => {
      const controller = new AbortController()
      setTimeout(() => controller.abort(new DOMException("Deadline exceeded", "TimeoutError")), milliseconds)
      return controller.signal
    })
  }

  it("permits the chain's sequential provider fallback to finish after 121 seconds", async () => {
    fakeDeadlines()
    vi.stubGlobal("fetch", vi.fn((_input, init?: RequestInit) => new Promise<Response>((resolve, reject) => {
      init!.signal!.addEventListener("abort", () => reject(init!.signal!.reason), { once: true })
      setTimeout(() => resolve(new Response(JSON.stringify(samplePage()))), 121_000)
    })))
    const result = expect(fetchCoveredCalls("IREN", "itm")).resolves.toMatchObject({ ticker: "IREN" })
    await vi.advanceTimersByTimeAsync(121_000)
    await result
  })

  it.each([
    ["calls", () => fetchCoveredCalls("IREN", "itm"), 150_000],
    ["puts", () => fetchChain("IREN", "put", "otm"), 150_000],
    ["tickers", () => fetchTickers("IRE"), 30_000],
    ["watchlist", () => getWatchlist(), 60_000],
  ] as const)("expires %s at its bounded deadline", async (_label, request, milliseconds) => {
    fakeDeadlines()
    vi.stubGlobal("fetch", vi.fn((_input, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init!.signal!.addEventListener("abort", () => reject(init!.signal!.reason), { once: true })
    })))
    let settled = false
    const pending = request().finally(() => { settled = true })
    const result = expect(pending).rejects.toThrow("The API did not respond.")
    expect(AbortSignal.timeout).toHaveBeenCalledWith(milliseconds)
    await vi.advanceTimersByTimeAsync(milliseconds - 1)
    expect(settled).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await result
  })

  it.each([
    ["chain", () => fetchCoveredCalls("IREN", "itm"), 150_000],
    ["watchlist", () => getWatchlist(), 60_000],
  ] as const)("normalizes a %s timeout while consuming the response body", async (_label, request, milliseconds) => {
    fakeDeadlines()
    vi.stubGlobal("fetch", vi.fn(async (_input, init?: RequestInit) => ({
      ok: true,
      json: () => new Promise((_resolve, reject) => {
        init!.signal!.addEventListener("abort", () => reject(new DOMException("Body aborted", "AbortError")), { once: true })
      }),
    })))
    const result = expect(request()).rejects.toThrow("The API did not respond.")
    await vi.advanceTimersByTimeAsync(milliseconds)
    await result
  })

  it("passes selection cancellation through both chain strategies", async () => {
    for (const side of ["call", "put"] as const) {
      const controller = new AbortController()
      const fetchMock = vi.fn((_input, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
        init!.signal!.addEventListener("abort", () => reject(init!.signal!.reason), { once: true })
      }))
      vi.stubGlobal("fetch", fetchMock)
      const result = expect(fetchChain("IREN", side, "itm", "lognormal_ewma", controller.signal)).rejects.toMatchObject({ name: "AbortError" })
      controller.abort()
      await result
    }
  })

  it("keeps the put page contract after adding a deadline", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(samplePutPage()))))
    await expect(fetchChain("IREN", "put", "otm")).resolves.toMatchObject({ moneyness: "otm" })
  })

  it("keeps frontend source off Nasdaq outside fixtures", () => {
    const root = join(dirname(fileURLToPath(import.meta.url)), "..")
    const matches: string[] = []
    const skip = new Set(["node_modules", "dist", "test-results", "playwright-report"])

    function walk(dir: string) {
      for (const name of readdirSync(dir)) {
        if (skip.has(name)) continue
        const path = join(dir, name)
        const stat = statSync(path)
        if (stat.isDirectory()) {
          walk(path)
          continue
        }
        if (!/\.(ts|tsx|js|jsx|css|html|json)$/.test(name)) continue
        if (name.includes(".test.") || name.includes(".spec.") || path.includes("/fixtures/") || path.includes("/e2e/")) {
          continue
        }
        const text = readFileSync(path, "utf8")
        if (text.includes("api.nasdaq.com")) matches.push(path)
      }
    }

    walk(root)
    expect(matches).toEqual([])
  })
})
