import { useCallback, useEffect, useRef, useState } from "react"

import { fetchChain } from "./api"
import { isRegularMarketHours } from "./marketHours"
import type { ChainPage, Moneyness, Side, Ticker } from "./types"

const REFRESH_MS = 5 * 60_000
const PENDING_MS = 15_000

export function useChainPage(ticker: Ticker, side: Side, moneyness: Moneyness) {
  const [page, setPage] = useState<ChainPage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const requestId = useRef(0)
  const lastRequestedAt = useRef(0)
  const requestKey = `${ticker}|${side}|${moneyness}`
  const [activeKey, setActiveKey] = useState(requestKey)
  if (activeKey !== requestKey) {
    setActiveKey(requestKey)
    requestId.current += 1
    setPage(null)
    setError(null)
    setLoading(true)
  }

  const load = useCallback(async (
    selected: Ticker,
    nextSide: Side,
    nextMoneyness: Moneyness,
    preservePage = false,
  ) => {
    const id = ++requestId.current
    lastRequestedAt.current = Date.now()
    try {
      const result = await fetchChain(selected, nextSide, nextMoneyness)
      if (id !== requestId.current) return
      setPage(result)
      setError(null)
    } catch (cause) {
      if (id !== requestId.current) return
      if (!preservePage) setPage(null)
      setError(cause instanceof Error ? cause.message : "Local API unavailable")
    } finally {
      if (id === requestId.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(ticker, side, moneyness)
  }, [load, ticker, side, moneyness])

  const pending = page?.expirations.some((group) => group.contracts.some(
    (row) => row.market_odds?.status === "pending",
  )) ?? false

  useEffect(() => {
    const interval = pending ? PENDING_MS : REFRESH_MS
    const refresh = () => {
      if (document.hidden || loading || (!pending && !isRegularMarketHours())) return
      if (Date.now() - lastRequestedAt.current < interval) return
      void load(ticker, side, moneyness, true)
    }
    const timer = window.setInterval(refresh, interval)
    document.addEventListener("visibilitychange", refresh)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener("visibilitychange", refresh)
    }
  }, [load, loading, moneyness, pending, side, ticker])

  const beginTickerChange = () => {
    setPage(null)
    setError(null)
    setLoading(true)
  }

  const beginRefresh = () => {
    setLoading(true)
    setError(null)
    void load(ticker, side, moneyness, true)
  }

  return { page, error, loading, beginTickerChange, beginRefresh }
}
