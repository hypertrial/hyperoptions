import { useCallback, useEffect, useRef, useState } from "react"

import { fetchChain } from "./api"
import type { ChainPage, Moneyness, Side, Ticker } from "./types"

export function useChainPage(ticker: Ticker, side: Side, moneyness: Moneyness) {
  const [page, setPage] = useState<ChainPage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const requestId = useRef(0)

  const load = useCallback(async (
    selected: Ticker,
    nextSide: Side,
    nextMoneyness: Moneyness,
    preservePage = false,
  ) => {
    const id = ++requestId.current
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
