import { useEffect, useMemo, useRef, useState } from "react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { CircleAlert, RefreshCw } from "lucide-react"
import CommandBar, { type SessionInfo } from "./CommandBar"
import { formatContractValues, visibleColumns } from "./columns"
import { contractCountIsSafe, contractSizeLabel, parseContractCount } from "./contracts"
import { copyRowStateKey, formatRowClipboard } from "./copyRow"
import { useDensity } from "./density"
import ExpiryTables from "./ExpiryTable"
import FilterControls from "./FilterControls"
import { parseThreshold } from "./filters"
import { useChainPage } from "./useChainPage"
import { defaultMoneyness, useUrlState } from "./useUrlState"
import { DEFAULT_SORT, deriveChainView, INITIAL_REVEAL, nearestMatchingExpiration, type SortState } from "./viewModel"
import type { Moneyness, Side } from "./types"

const OPEN_SESSIONS = new Set(["market", "regular market", "open"])

function sessionState(page: ReturnType<typeof useChainPage>["page"]): SessionInfo {
  const raw = page?.market_session?.trim()
  if (!raw) return { label: "Session unavailable", state: "unknown", detail: null }
  const normalized = raw.toLowerCase()
  if (OPEN_SESSIONS.has(normalized)) return { label: "Market open", state: "open", detail: null }
  if (normalized === "closed") return { label: "Market closed", state: "closed", detail: null }
  return { label: "Market closed", state: "closed", detail: raw }
}

function emptyCopy(ticker: string, side: Side, moneyness: Moneyness, optionsAvailable: boolean, priced: boolean) {
  if (!optionsAvailable) return `Options are not available for ${ticker}`
  if (!priced) return `No usable ${ticker} price is available, so contracts cannot be listed.`
  const kind = side === "put" ? "puts" : "calls"
  if (moneyness === "all") return `No ${kind} for ${ticker}`
  return `No ${moneyness.toUpperCase()} ${kind} for ${ticker}`
}

function fallbackSort(columns: ReturnType<typeof visibleColumns>): SortState {
  const id = columns[0]?.id ?? DEFAULT_SORT.id
  return { id, dir: id === "strike_cents" ? "desc" : "asc" }
}

export default function ItmChain() {
  const { state, setState } = useUrlState()
  const { ticker, side, moneyness, cols } = state
  const identityKey = `${ticker}|${side}|${moneyness}`
  const { page, error, loading, beginTickerChange, beginRefresh } = useChainPage(ticker, side, moneyness)
  const [contractsText, setContractsText] = useState("")
  const [minPrimaryText, setMinPrimaryText] = useState("")
  const [minAprText, setMinAprText] = useState("")
  const [minDropText, setMinDropText] = useState("")
  const [minDteText, setMinDteText] = useState("")
  const [maxDteText, setMaxDteText] = useState("")
  const [copiedKey, setCopiedKey] = useState<string | null>(null)
  const [copiedNotice, setCopiedNotice] = useState("")
  const [revealState, setRevealState] = useState({ key: "", limit: INITIAL_REVEAL })
  const [sort, setSort] = useState<SortState>(DEFAULT_SORT)
  const [expansionState, setExpansionState] = useState<{
    identity: string
    values: Set<string> | null
    seed: { primary: string; apr: string; drop: string; minDte: string; maxDte: string; contracts: number }
  }>(() => ({
    identity: identityKey,
    values: null,
    seed: { primary: "", apr: "", drop: "", minDte: "", maxDte: "", contracts: 1 },
  }))
  const { density, toggle: toggleDensity } = useDensity()
  const copiedClear = useRef<number | null>(null)
  const columns = visibleColumns(side, cols)
  const filterKey = `${identityKey}|${minPrimaryText}|${minAprText}|${minDropText}|${minDteText}|${maxDteText}`
  const revealLimit = revealState.key === filterKey ? revealState.limit : INITIAL_REVEAL
  const effectiveSort = columns.some((column) => column.id === sort.id) ? sort : fallbackSort(columns)

  const updateRevealLimit = (next: number | ((current: number) => number)) => {
    const limit = typeof next === "function" ? next(revealLimit) : next
    setRevealState({ key: filterKey, limit })
  }

  useEffect(() => () => {
    if (copiedClear.current != null) window.clearTimeout(copiedClear.current)
  }, [])

  const session = sessionState(page)
  const loadingNewPage = loading && page == null
  const pageUnavailable = !loading && error != null && page == null
  const currentSource = page?.current_source === "stock_bid"
    ? "Stock bid"
    : page?.current_source === "chain_last_trade"
      ? "Chain last trade"
      : "No usable price"
  const quoteStamp = page?.current_source === "stock_bid"
    ? page.quote_timestamp?.trim()
    : page?.current_source === "chain_last_trade"
      ? page.last_trade_timestamp?.trim()
      : undefined
  const parsedContracts = parseContractCount(contractsText)
  const contractsUnsafe = parsedContracts != null && !contractCountIsSafe(parsedContracts, page)
  const contractsInvalid = contractsText.trim() !== "" && (parsedContracts == null || contractsUnsafe)
  const contracts = parsedContracts != null && !contractsUnsafe ? parsedContracts : 1
  const contractsHelp = contractsInvalid
    ? contractsUnsafe
      ? "This contract count is too large to calculate exactly. Using 1 contract."
      : "Enter a whole number of 1 or more. Using 1 contract."
    : contractSizeLabel(contracts, page?.current_cents)
  const minPrimary = parseThreshold(minPrimaryText)
  const minApr = parseThreshold(minAprText)
  const minDrop = parseThreshold(minDropText)
  const minDte = parseThreshold(minDteText)
  const maxDte = parseThreshold(maxDteText)
  const filters = { primary: minPrimary, apr: minApr, drop: minDrop, minDte, maxDte }
  const seededExpiration = useMemo(() => {
    const seed = expansionState.identity === identityKey
      ? expansionState.seed
      : {
          primary: minPrimaryText,
          apr: minAprText,
          drop: minDropText,
          minDte: minDteText,
          maxDte: maxDteText,
          contracts,
        }
    return nearestMatchingExpiration(page, seed.contracts, {
      primary: parseThreshold(seed.primary),
      apr: parseThreshold(seed.apr),
      drop: parseThreshold(seed.drop),
      minDte: parseThreshold(seed.minDte),
      maxDte: parseThreshold(seed.maxDte),
    }, side)
  }, [
    page,
    side,
    identityKey,
    expansionState,
    minPrimaryText,
    minAprText,
    minDropText,
    minDteText,
    maxDteText,
    contracts,
  ])
  const storedExpandedExpirations = expansionState.identity === identityKey ? expansionState.values : null
  const expandedExpirations = useMemo(
    () => storedExpandedExpirations ?? new Set(seededExpiration ? [seededExpiration] : []),
    [seededExpiration, storedExpandedExpirations],
  )
  const view = deriveChainView(page, contracts, filters, revealLimit, side, columns, effectiveSort, expandedExpirations)
  const strategyLabel = side === "put" ? "Cash-secured puts" : "Covered calls"
  const expandedVisibleCount = view.visibleGroups.filter((item) => expandedExpirations.has(item.group.expiration)).length

  const currentExpansionSeed = () => ({
    primary: minPrimaryText,
    apr: minAprText,
    drop: minDropText,
    minDte: minDteText,
    maxDte: maxDteText,
    contracts,
  })

  const resetExpansion = (nextIdentity: string) => {
    setExpansionState({ identity: nextIdentity, values: null, seed: currentExpansionSeed() })
  }

  const updateExpandedExpirations = (next: Set<string> | ((current: Set<string>) => Set<string>)) => {
    const values = typeof next === "function" ? next(expandedExpirations) : next
    setExpansionState({
      identity: identityKey,
      values,
      seed: expansionState.identity === identityKey ? expansionState.seed : currentExpansionSeed(),
    })
  }

  const selectTicker = (item: string) => {
    if (item === ticker) return
    if (copiedClear.current != null) window.clearTimeout(copiedClear.current)
    setCopiedKey(null)
    setCopiedNotice("")
    resetExpansion(`${item}|${side}|${moneyness}`)
    beginTickerChange()
    setState({ ...state, ticker: item })
  }

  const selectSide = (next: Side) => {
    if (next === side) return
    const nextMoneyness = state.moneyness === "all" ? "all" : defaultMoneyness(next)
    resetExpansion(`${ticker}|${next}|${nextMoneyness}`)
    beginTickerChange()
    setSort(DEFAULT_SORT)
    setState({ ...state, side: next, moneyness: nextMoneyness, cols: null })
  }

  const selectMoneyness = (next: Moneyness) => {
    if (next === moneyness) return
    resetExpansion(`${ticker}|${side}|${next}`)
    beginTickerChange()
    setState({ ...state, moneyness: next })
  }

  const selectSort = (id: string) => {
    setSort((current) => {
      const normalized = columns.some((column) => column.id === current.id) ? current : fallbackSort(columns)
      return normalized.id === id
        ? { id, dir: normalized.dir === "desc" ? "asc" : "desc" }
        : { id, dir: id === "strike_cents" ? "desc" : "asc" }
    })
  }

  const selectColumns = (next: string[] | null) => {
    const nextColumns = visibleColumns(side, next)
    if (!nextColumns.some((column) => column.id === sort.id)) setSort(fallbackSort(nextColumns))
    setState({ ...state, cols: next })
  }

  const markCopied = (key: string) => {
    setCopiedKey(key)
    setCopiedNotice("Copied")
    if (copiedClear.current != null) window.clearTimeout(copiedClear.current)
    copiedClear.current = window.setTimeout(() => {
      setCopiedKey(null)
      setCopiedNotice("")
      copiedClear.current = null
    }, 1500)
  }

  const copyVisibleRow = async (row: { expiration: string; strike_cents: number }, group: { expiration: string; dte: number }) => {
    const text = formatRowClipboard(
      {
        ticker,
        expiration: group.expiration,
        dte: group.dte,
        currentSource,
        currentCents: page?.current_cents,
        contracts,
      },
      columns.map((column) => column.label),
      formatContractValues(row as never, columns),
    )
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      return
    }
    markCopied(copyRowStateKey(ticker, row.expiration, row.strike_cents))
  }

  const clearFilters = () => {
    setMinPrimaryText("")
    setMinAprText("")
    setMinDropText("")
    setMinDteText("")
    setMaxDteText("")
  }

  return (
    <div className="chain-shell">
      <CommandBar
        ticker={ticker}
        side={side}
        moneyness={moneyness}
        page={page}
        session={session}
        currentSource={currentSource}
        quoteStamp={quoteStamp}
        contractsText={contractsText}
        contractsInvalid={contractsInvalid}
        contractsHelp={contractsHelp}
        loading={loading}
        onSelectTicker={selectTicker}
        onSelectSide={selectSide}
        onSelectMoneyness={selectMoneyness}
        onContractsChange={setContractsText}
        onRefresh={beginRefresh}
      />

      <div className="results-canvas">
        <header className="chain-header">
          <p className="eyebrow">Option chain</p>
          <h1>{strategyLabel}</h1>
          <p className="chain-context">
            {ticker} <span aria-hidden="true">·</span> {moneyness.toUpperCase()}
            {loadingNewPage ? (
              <><span aria-hidden="true">·</span> Loading market data…</>
            ) : pageUnavailable ? (
              <><span aria-hidden="true">·</span> Data unavailable</>
            ) : (
              <>
                <span aria-hidden="true">·</span> {view.visibleCount.toLocaleString("en-US")} contracts
                <span aria-hidden="true">·</span> {view.visibleGroups.length.toLocaleString("en-US")} expirations
              </>
            )}
          </p>
        </header>

        <main id="main-content" className="chain-panel" aria-busy={loading}>
          {page ? (
            <FilterControls
              side={side}
              selectedColumns={cols}
              density={density}
              visibleCount={view.visibleCount}
              expirationCount={view.visibleGroups.length}
              expandedCount={expandedVisibleCount}
              minPrimaryText={minPrimaryText}
              minAprText={minAprText}
              minDropText={minDropText}
              minDteText={minDteText}
              maxDteText={maxDteText}
              minPrimary={minPrimary}
              minApr={minApr}
              minDrop={minDrop}
              minDte={minDte}
              maxDte={maxDte}
              invertedDte={view.invertedDte}
              onChangeColumns={selectColumns}
              onToggleDensity={toggleDensity}
              onExpandAll={() => updateExpandedExpirations(new Set(view.visibleGroups.map((item) => item.group.expiration)))}
              onCollapseAll={() => updateExpandedExpirations(new Set())}
              onMinPrimaryChange={setMinPrimaryText}
              onMinAprChange={setMinAprText}
              onMinDropChange={setMinDropText}
              onMinDteChange={setMinDteText}
              onMaxDteChange={setMaxDteText}
              onClearFilters={clearFilters}
            />
          ) : null}

          {page?.truncated ? (
            <Alert role="status" className="banner">
              <AlertDescription>Nasdaq returned a truncated chain. Far expirations may be missing.</AlertDescription>
            </Alert>
          ) : null}
          {error && page ? (
            <Alert role="alert" className="banner stale-warning">
              <AlertDescription>
                <span><strong>Refresh failed.</strong> Showing the last successful chain. {error}</span>
                <Button type="button" variant="outline" size="sm" onClick={beginRefresh}>Retry</Button>
              </AlertDescription>
            </Alert>
          ) : null}
          {pageUnavailable ? (
            <div className="error-state" role="alert">
              <div className="error-state-heading">
                <CircleAlert aria-hidden="true" />
                <div>
                  <strong>Couldn’t load {ticker} market data</strong>
                  <p>{error}</p>
                </div>
              </div>
              <p>Check the ticker selection or try the request again.</p>
              <Button type="button" variant="outline" onClick={beginRefresh}>Try again</Button>
            </div>
          ) : null}
          {loadingNewPage ? (
            <div className="loading-state" role="status" aria-live="polite">
              <div className="loading-state-heading">
                <RefreshCw className="animate-spin" aria-hidden="true" />
                <div>
                  <strong>Loading {ticker} market data</strong>
                  <p>Fetching the latest quote and option chain. This can take a few seconds.</p>
                </div>
              </div>
              <div className="loading-state-skeletons" aria-hidden="true">
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-3/4" />
              </div>
            </div>
          ) : null}
          {!loading && page && view.providerCount === 0 ? (
            <div className="empty-state" role="status">
              <p>{emptyCopy(ticker, side, moneyness, page.options_available, page.current_cents != null)}</p>
            </div>
          ) : null}
          {!loading && view.filterMiss ? (
            <div className="empty-state" role="status">
              <p>No rows match the current filters.</p>
            </div>
          ) : null}

          <p className="visually-hidden" role="status" aria-live="polite">{copiedNotice}</p>
          {view.remainingCount > 0 ? (
            <div className="reveal-cluster">
              <p className="control-note">Displaying {view.mountedCount.toLocaleString("en-US")} of {view.expandedVisibleCount.toLocaleString("en-US")} rows in expanded expirations</p>
              <Button type="button" variant="outline" onClick={() => updateRevealLimit((current) => current + INITIAL_REVEAL)}>
                Show {INITIAL_REVEAL} more
              </Button>
              <Button type="button" variant="outline" onClick={() => updateRevealLimit(Number.POSITIVE_INFINITY)}>
                Show all
              </Button>
            </div>
          ) : null}

          <ExpiryTables
            ticker={ticker}
            side={side}
            columns={columns}
            groups={view.visibleGroups}
            mountedGroups={view.mountedGroups}
            expandedExpirations={expandedExpirations}
            copiedKey={copiedKey}
            sort={effectiveSort}
            density={density}
            onToggleExpiration={(expiration) => updateExpandedExpirations((current) => {
              const next = new Set(current)
              if (next.has(expiration)) next.delete(expiration)
              else next.add(expiration)
              return next
            })}
            onSort={selectSort}
            onCopy={(row, group) => void copyVisibleRow(row, group)}
          />
        </main>
      </div>
    </div>
  )
}
