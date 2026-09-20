import { zCashSecuredPutPage, zCoveredCallPage, zTickerSearchResponse } from "./generated/zod.gen"
import type { CashSecuredPutPage, CoveredCallPage, Moneyness, Side, Ticker, TickerSearchResponse } from "./types"

function errorMessage(status: number, body: unknown): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === "string" && detail.trim()) return detail
  }
  if (status === 504) return "Nasdaq timeout"
  if (status === 502) return "Nasdaq unavailable"
  if (status === 503) return "Ticker universe unavailable"
  return `Request failed (${status})`
}

async function fetchJson(url: string, init?: RequestInit): Promise<unknown> {
  let response: Response
  try {
    response = await fetch(url, init)
  } catch {
    throw new Error("Local API unavailable")
  }
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    // Status still provides a useful fallback error.
  }
  if (!response.ok) throw new Error(errorMessage(response.status, body))
  if (!body || typeof body !== "object") {
    throw new Error("Local API returned an invalid response")
  }
  return body
}

export async function fetchTickers(query: string, limit = 10): Promise<TickerSearchResponse> {
  const params = new URLSearchParams()
  if (query) params.set("q", query.slice(0, 32))
  params.set("limit", String(limit))
  const body = await fetchJson(`/api/tickers?${params.toString()}`)
  const parsed = zTickerSearchResponse.safeParse(body)
  if (!parsed.success) {
    throw new Error("Local API returned an invalid response")
  }
  return parsed.data
}

export async function fetchCoveredCalls(ticker: Ticker, moneyness: Moneyness): Promise<CoveredCallPage> {
  const body = await fetchJson(`/api/covered-calls/${ticker}?moneyness=${moneyness}`)
  const parsed = zCoveredCallPage.safeParse(body)
  if (!parsed.success) {
    throw new Error("Local API returned an invalid response")
  }
  return parsed.data
}

export async function fetchCashSecuredPuts(ticker: Ticker, moneyness: Moneyness): Promise<CashSecuredPutPage> {
  const body = await fetchJson(`/api/cash-secured-puts/${ticker}?moneyness=${moneyness}`)
  const parsed = zCashSecuredPutPage.safeParse(body)
  if (!parsed.success) {
    throw new Error("Local API returned an invalid response")
  }
  return parsed.data
}

export async function fetchChain(ticker: Ticker, side: Side, moneyness: Moneyness) {
  return side === "put"
    ? fetchCashSecuredPuts(ticker, moneyness)
    : fetchCoveredCalls(ticker, moneyness)
}
