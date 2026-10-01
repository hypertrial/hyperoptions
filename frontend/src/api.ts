import { zCashSecuredPutPage, zCoveredCallPage, zTickerSearchResponse } from "./generated/zod.gen"
import { DEFAULT_FORECAST_MODEL, type PhysicalModel } from "./forecastModels"
import type { CashSecuredPutPage, CoveredCallPage, Moneyness, Side, Ticker, TickerSearchResponse } from "./types"
import { boundedFetch } from "./boundedFetch"

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

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

async function fetchJson(url: string, init?: RequestInit, timeoutMs = 150_000): Promise<unknown> {
  let response: Response
  let body: unknown = null
  try {
    response = await boundedFetch(url, init, timeoutMs)
    try {
      body = await response.json()
    } catch (cause) {
      if (init?.signal?.aborted || (cause instanceof Error && (cause.name === "AbortError" || cause.name === "TimeoutError"))) throw cause
      // Status still provides a useful fallback error.
    }
  } catch (cause) {
    if (init?.signal?.aborted) throw cause
    if (cause instanceof Error && (cause.name === "AbortError" || cause.name === "TimeoutError")) {
      throw new Error("The API did not respond.")
    }
    throw new Error("Local API unavailable")
  }
  if (!response.ok) throw new ApiError(response.status, errorMessage(response.status, body))
  if (!body || typeof body !== "object") {
    throw new Error("Local API returned an invalid response")
  }
  return body
}

export async function fetchTickers(query: string, limit = 10, signal?: AbortSignal): Promise<TickerSearchResponse> {
  const params = new URLSearchParams()
  if (query) params.set("q", query.slice(0, 32))
  params.set("limit", String(limit))
  const body = await fetchJson(`/api/tickers?${params.toString()}`, { signal }, 30_000)
  const parsed = zTickerSearchResponse.safeParse(body)
  if (!parsed.success) {
    throw new Error("Local API returned an invalid response")
  }
  return parsed.data
}

export async function fetchCoveredCalls(ticker: Ticker, moneyness: Moneyness, model: PhysicalModel = "lognormal_ewma", signal?: AbortSignal): Promise<CoveredCallPage> {
  const choice = model === DEFAULT_FORECAST_MODEL ? "" : `&forecast_model=${model}`
  const body = await fetchJson(`/api/covered-calls/${ticker}?moneyness=${moneyness}${choice}`, { signal })
  const parsed = zCoveredCallPage.safeParse(body)
  if (!parsed.success) {
    throw new Error("Local API returned an invalid response")
  }
  return parsed.data
}

export async function fetchCashSecuredPuts(ticker: Ticker, moneyness: Moneyness, model: PhysicalModel = "lognormal_ewma", signal?: AbortSignal): Promise<CashSecuredPutPage> {
  const choice = model === DEFAULT_FORECAST_MODEL ? "" : `&forecast_model=${model}`
  const body = await fetchJson(`/api/cash-secured-puts/${ticker}?moneyness=${moneyness}${choice}`, { signal })
  const parsed = zCashSecuredPutPage.safeParse(body)
  if (!parsed.success) {
    throw new Error("Local API returned an invalid response")
  }
  return parsed.data
}

export async function fetchChain(ticker: Ticker, side: Side, moneyness: Moneyness, model: PhysicalModel = "lognormal_ewma", signal?: AbortSignal) {
  return side === "put"
    ? fetchCashSecuredPuts(ticker, moneyness, model, signal)
    : fetchCoveredCalls(ticker, moneyness, model, signal)
}
