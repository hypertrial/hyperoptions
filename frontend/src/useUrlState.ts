import { useCallback, useEffect } from "react"
import { useSearchParams } from "react-router-dom"

import { normalizeColumnIds } from "./columns"
import type { Moneyness, Side } from "./types"

const TICKER = /^[A-Z]{1,5}$/

export type UrlState = {
  ticker: string
  side: Side
  moneyness: Moneyness
  cols: string[] | null
}

export function defaultMoneyness(side: Side): Moneyness {
  return side === "put" ? "otm" : "itm"
}

export function parseUrlState(search = typeof window === "undefined" ? "" : window.location.search): UrlState {
  const params = new URLSearchParams(search)
  const rawTicker = (params.get("t") ?? "IREN").trim().toUpperCase()
  const ticker = TICKER.test(rawTicker) ? rawTicker : "IREN"
  const side: Side = params.get("side") === "put" ? "put" : "call"
  const rawM = params.get("m")
  const moneyness: Moneyness =
    rawM === "itm" || rawM === "otm" || rawM === "all" ? rawM : defaultMoneyness(side)
  const rawCols = params.get("cols")
  const parsedCols = rawCols == null || rawCols.trim() === ""
    ? null
    : rawCols.split(",").map((item) => item.trim()).filter(Boolean)
  const cols = normalizeColumnIds(side, parsedCols)
  return { ticker, side, moneyness, cols }
}

export function serializeUrlState(state: UrlState): string {
  const params = new URLSearchParams()
  params.set("t", state.ticker)
  params.set("side", state.side)
  params.set("m", state.moneyness)
  if (state.cols && state.cols.length > 0) params.set("cols", state.cols.join(","))
  return `?${params.toString()}`
}

export function useUrlState() {
  const [params, setParams] = useSearchParams()
  const state = parseUrlState(params.toString())
  const canonical = serializeUrlState(state).slice(1)

  useEffect(() => {
    if (params.toString() !== canonical) setParams(canonical, { replace: true })
  }, [canonical, params, setParams])

  const setState = useCallback((next: UrlState) => {
    setParams(serializeUrlState(next).slice(1), { replace: true })
  }, [setParams])

  return { state, setState }
}
