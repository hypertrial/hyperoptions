import { readdirSync, readFileSync, statSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError, fetchChain, fetchCoveredCalls, fetchTickers } from "./api"
import { samplePage } from "./testFixtures"

describe("chain API", () => {
  afterEach(() => vi.unstubAllGlobals())

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
