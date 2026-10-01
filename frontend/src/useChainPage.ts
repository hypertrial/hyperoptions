import { useCallback, useEffect, useRef, useState } from "react"

import { fetchChain } from "./api"
import type { PhysicalModel } from "./forecastModels"
import { isRegularMarketHours } from "./marketHours"
import type { ChainPage, Moneyness, Side, Ticker } from "./types"

const REFRESH_MS = 5 * 60_000
const PENDING_MS = 15_000
const MAX_EVIDENCE_POLLS = 8

type PendingLoad = { key: string; id: number; controller: AbortController; promise: Promise<void> }

export function useChainPage(ticker: Ticker, side: Side, moneyness: Moneyness, forecastModel: PhysicalModel = "lognormal_ewma") {
  const [page, setPage] = useState<ChainPage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const requestId = useRef(0)
  const lastRequestedAt = useRef(0)
  const pendingLoad = useRef<PendingLoad | null>(null)
  const requestKey = `${ticker}|${side}|${moneyness}|${forecastModel}`
  const [evidencePolls, setEvidencePolls] = useState({ key: requestKey, count: 0 })
  const [activeKey, setActiveKey] = useState(requestKey)
  if (activeKey !== requestKey) {
    setActiveKey(requestKey)
    requestId.current += 1
    setPage(null)
    setError(null)
    setLoading(true)
  }

  const load = useCallback((
    selected: Ticker,
    nextSide: Side,
    nextMoneyness: Moneyness,
    nextModel: PhysicalModel,
    preservePage = false,
  ) => {
    const key = `${selected}|${nextSide}|${nextMoneyness}|${nextModel}`
    if (pendingLoad.current?.key === key) return pendingLoad.current.promise
    const id = ++requestId.current
    const current: PendingLoad = { key, id, controller: new AbortController(), promise: Promise.resolve() }
    pendingLoad.current = current
    lastRequestedAt.current = Date.now()
    current.promise = (async () => {
      try {
        const result = await fetchChain(selected, nextSide, nextMoneyness, nextModel, current.controller.signal)
        if (id !== requestId.current || current.controller.signal.aborted) return
        setPage(result)
        setError(null)
      } catch (cause) {
        if (id !== requestId.current || current.controller.signal.aborted) return
        if (!preservePage) setPage(null)
        setError(cause instanceof Error ? cause.message : "Local API unavailable")
      } finally {
        if (pendingLoad.current === current) pendingLoad.current = null
        if (id === requestId.current) setLoading(false)
      }
    })()
    return current.promise
  }, [])

  useEffect(() => {
    const key = `${ticker}|${side}|${moneyness}|${forecastModel}`
    void load(ticker, side, moneyness, forecastModel)
    return () => {
      requestId.current += 1
      const current = pendingLoad.current
      if (current?.key === key) {
        pendingLoad.current = null
        current.controller.abort()
      }
    }
  }, [load, ticker, side, moneyness, forecastModel])

  const pending = page?.expirations.some((group) => group.contracts.some(
    (row) => row.market_odds?.status === "pending"
      || row.predictive_odds?.status === "pending"
      || row.physical_models?.some((model) => model.status === "pending")
      || row.market_models?.some((model) => model.status === "pending"),
  )) ?? false
  const retryableForecast = page?.expirations.some((group) => group.contracts.some(
    (row) => row.predictive_odds?.status === "unavailable"
      && (row.predictive_odds.reason === "market_data_missing" || row.predictive_odds.reason === "market_data_invalid"),
  )) ?? false
  const missingEvidence = page?.expirations.some((group) => group.contracts.some(
    (row) => row.physical_models?.some((model) => model.evidence_key && !page.model_evidence?.[model.evidence_key]),
  )) ?? false
  const evidencePending = missingEvidence && (evidencePolls.key !== requestKey || evidencePolls.count < MAX_EVIDENCE_POLLS)

  useEffect(() => {
    const interval = pending || evidencePending ? PENDING_MS : REFRESH_MS
    const refresh = () => {
      if (document.hidden || loading || (!pending && !evidencePending && !retryableForecast && !isRegularMarketHours())) return
      if (pendingLoad.current?.key === requestKey) return
      if (Date.now() - lastRequestedAt.current < interval) return
      if (evidencePending && !pending) setEvidencePolls((current) => ({
        key: requestKey,
        count: (current.key === requestKey ? current.count : 0) + 1,
      }))
      void load(ticker, side, moneyness, forecastModel, true)
    }
    const timer = window.setInterval(refresh, interval)
    document.addEventListener("visibilitychange", refresh)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener("visibilitychange", refresh)
    }
  }, [load, loading, moneyness, pending, evidencePending, retryableForecast, side, ticker, forecastModel, requestKey])

  const beginTickerChange = () => {
    setPage(null)
    setError(null)
    setLoading(true)
  }

  const beginRefresh = () => {
    setEvidencePolls({ key: requestKey, count: 0 })
    setLoading(true)
    setError(null)
    void load(ticker, side, moneyness, forecastModel, true)
  }

  return { page, error, loading, beginTickerChange, beginRefresh }
}
