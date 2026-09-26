import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { useMemo } from "react"
import { Link, useOutletContext, useSearchParams } from "react-router-dom"
import type { LegacyColumnDef } from "@tanstack/react-table/legacy"
import { getJson } from "../api/client"
import type { ConfigView, CrossTickerItem, Leaderboard as Board, LeaderboardItem, RunTicker } from "../api/types"
import { DataTable } from "../components/DataTable"
import { RuleLines } from "../components/WindowStats"
import { EmptyState } from "../components/ui/empty-state"
import { Alert } from "../components/ui/notice"
import { Skeleton } from "../components/ui/skeleton"
import { InfoHint } from "../components/ui/info-hint"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs"
import { useJob } from "../jobs/JobProvider"
import { formatNum, formatPct, formatSignedPct, tone } from "../lib/format"
import { EXITS, exitLabel, familyLabel, flagLabel, gatesFromConfig } from "../lib/labels"
import {
  LEADERBOARD_PAGE_SIZE,
  LEADERBOARD_SORTS,
  applySort,
  clearLeaderboardFilters,
  leaderboardQuery,
  leaderboardSortable,
  pageBounds,
  parseLeaderboardState,
  rankColumnLabel,
  serializeLeaderboardState,
  toggleSort,
  withFilter,
  type LeaderboardState,
} from "../lib/leaderboard"
import { cn } from "../lib/utils"
import { useDocumentTitle } from "../lib/useDocumentTitle"

type OutletContext = { runId: string }

export function Leaderboard() {
  useDocumentTitle("Research leaderboard · HyperOptions")
  const { runId } = useOutletContext<OutletContext>()
  const [params, setParams] = useSearchParams()
  const state = parseLeaderboardState(params)
  const { start, busy } = useJob()
  const config = useQuery({
    queryKey: ["config"],
    queryFn: () => getJson<ConfigView>("/api/research/config"),
  })
  const queryText = leaderboardQuery(state, runId || undefined)
  const board = useQuery({
    queryKey: ["leaderboard", queryText],
    queryFn: () => getJson<Board>(`/api/research/leaderboard?${queryText}`),
    enabled: Boolean(runId),
    placeholderData: keepPreviousData,
  })
  const cross = useQuery({
    queryKey: ["cross", runId],
    queryFn: () => getJson<CrossTickerItem[]>(`/api/research/cross-ticker?run_id=${runId}`),
    enabled: Boolean(runId) && state.view === "cross",
  })
  const coverage = useQuery({
    queryKey: ["run-tickers", runId],
    queryFn: () => getJson<RunTicker[]>(`/api/research/runs/${runId}/tickers`),
    enabled: Boolean(runId) && state.view === "cross",
  })
  const gates = config.data ? gatesFromConfig(config.data.gates) : undefined
  const columns = useMemo(
    () =>
      leaderboardColumns(state, runId, gates, (rule) => {
        setParams(serializeLeaderboardState(withFilter(state, { rule, group: false }), runId))
      }),
    [state, runId, gates, setParams],
  )
  const page = pageBounds(state.offset, board.data?.total ?? 0)
  const tickers = config.data?.tickers
  const crossColumns = useMemo(() => crossTickerColumns(tickers ?? [], runId), [tickers, runId])

  function update(next: LeaderboardState) {
    setParams(serializeLeaderboardState(next, runId))
  }

  return (
    <div>
      <h1 className="mb-2">Strategy leaderboard</h1>
      <p className="mb-4 text-muted-foreground">
        Validation metrics can sort the table. Holdout CAGR is shown and never used to rank.
      </p>
      {!runId ? (
        <EmptyState title="No backtest yet">
          <button type="button" className="text-foreground hover:underline" disabled={busy} onClick={() => start("/api/research/backtest/run", {})}>
            Run backtest
          </button>
        </EmptyState>
      ) : null}
      <Tabs value={state.view} onValueChange={(view) => update({ ...state, view: view === "cross" ? "cross" : "leaderboard" })}>
        <TabsList>
          <TabsTrigger value="leaderboard">Leaderboard</TabsTrigger>
          <TabsTrigger value="cross">Cross-Ticker Strategies</TabsTrigger>
        </TabsList>
        <TabsContent value="leaderboard" className="mt-4">
          <div className="mb-4 flex flex-wrap items-center gap-2 text-sm">
            <select aria-label="Ticker" className="min-h-9 rounded-md border border-border bg-card px-2 py-1" value={state.ticker} onChange={(event) => update(withFilter(state, { ticker: event.target.value }))}>
              <option value="">All tickers</option>
              {(tickers ?? []).map((ticker) => (
                <option key={ticker}>{ticker}</option>
              ))}
            </select>
            <select aria-label="Family" className="min-h-9 rounded-md border border-border bg-card px-2 py-1" value={state.family} onChange={(event) => update(withFilter(state, { family: event.target.value }))}>
              <option value="">All families</option>
              {(board.data?.families ?? []).map((family) => (
                <option key={family} value={family}>
                  {familyLabel(family)}
                </option>
              ))}
            </select>
            <select aria-label="Exit" className="min-h-9 rounded-md border border-border bg-card px-2 py-1" value={state.exit} onChange={(event) => update(withFilter(state, { exit: event.target.value }))}>
              <option value="">All exits</option>
              {EXITS.map((kind) => (
                <option key={kind} value={kind}>
                  {exitLabel(kind)}
                </option>
              ))}
            </select>
            <select aria-label="Filters" className="min-h-9 rounded-md border border-border bg-card px-2 py-1" value={state.filters} onChange={(event) => update(withFilter(state, { filters: event.target.value }))}>
              <option value="">Any filters</option>
              <option value="0">Signal only</option>
              <option value="1">One filter</option>
              <option value="2">Two filters</option>
            </select>
            <input
              aria-label="Minimum trades"
              className="min-h-9 w-32 rounded-md border border-border bg-card px-2 py-1"
              inputMode="numeric"
              placeholder="Min trades"
              value={state.minTrades}
              onChange={(event) => {
                const value = event.target.value
                if (value === "" || /^\d+$/.test(value)) update(withFilter(state, { minTrades: value }))
              }}
            />
            <label className="inline-flex min-h-9 items-center gap-2">
              <input
                type="checkbox"
                className="size-6"
                checked={state.includeRejected}
                onChange={(event) => update(withFilter(state, { includeRejected: event.target.checked }))}
              />
              Show rejected
            </label>
            <label className="inline-flex min-h-9 items-center gap-2">
              <input
                type="checkbox"
                className="size-6"
                checked={state.group}
                onChange={(event) => update(withFilter(state, { group: event.target.checked }))}
              />
              Group variants
            </label>
            {state.rule ? (
              <button type="button" className="inline-flex min-h-6 items-center rounded-full border border-border bg-muted px-2 py-0.5 text-xs" onClick={() => update(withFilter(state, { rule: "", group: true }))}>
                {state.rule} ×
              </button>
            ) : null}
            <button type="button" className="inline-flex min-h-6 items-center text-xs text-muted-foreground hover:text-foreground" onClick={() => update(clearLeaderboardFilters(state))}>
              Clear filters
            </button>
          </div>
          {state.group ? (
            <p className="mb-3 text-xs text-muted-foreground">Grouped rows keep the best variant&apos;s rank, so the numbers can skip.</p>
          ) : null}
          {runId ? (
          <>
          {board.isLoading && !board.data ? <Skeleton className="h-40" /> : board.error && !board.data ? <Alert>{board.error instanceof Error ? board.error.message : "Leaderboard failed."}</Alert> : (
          <>
          <LeaderboardCards
            items={board.data?.items ?? []}
            state={state}
            runId={runId}
            gates={gates}
            onVariants={(rule) => update(withFilter(state, { rule, group: false }))}
            onSort={(column) => update(applySort(state, column))}
            onToggleOrder={() => update(toggleSort(state, state.sort))}
          />
          <div className="hidden md:block">
          <DataTable
            caption="Strategies ranked on the validation window"
            data={board.data?.items ?? []}
            columns={columns}
            sort={state.sort}
            order={state.order}
            canSort={leaderboardSortable}
            onSort={(id) => update(toggleSort(state, id))}
            groups={[
              { label: "", span: 4 },
              { label: "Validation", span: 5 },
              { label: "Holdout", span: 1, className: "border-l border-border" },
            ]}
            rowClassName={(row) => (row.rejected ? "bg-muted/80 text-muted-foreground" : "")}
            empty={runId ? "No strategies match these filters." : "Run a backtest to fill the leaderboard."}
          />
          </div>
          <div className="mt-3 flex items-center justify-between text-sm text-muted-foreground">
            <span>
              {page.from}-{page.to} of {board.data?.total ?? 0}
            </span>
            <span className="flex gap-2">
              <button type="button" className="inline-flex min-h-9 items-center rounded-md border border-border px-3 py-1 disabled:opacity-40" disabled={!page.hasPrev} onClick={() => update({ ...state, offset: Math.max(0, state.offset - LEADERBOARD_PAGE_SIZE) })}>
                Previous
              </button>
              <button type="button" className="inline-flex min-h-9 items-center rounded-md border border-border px-3 py-1 disabled:opacity-40" disabled={!page.hasNext} onClick={() => update({ ...state, offset: state.offset + LEADERBOARD_PAGE_SIZE })}>
                Next
              </button>
            </span>
          </div>
          </>
          )}
          </>
          ) : null}
        </TabsContent>
        <TabsContent value="cross" className="mt-4">
          {cross.isLoading ? (
            <Skeleton className="h-40" />
          ) : (cross.data ?? []).length === 0 ? (
            <EmptyState title="No rule qualified on enough full-history tickers">
              <p>Limited-history tickers do not veto a rule. Survivors in this run:</p>
              <ul className="mt-2 space-y-1">
                {(coverage.data ?? []).map((item) => (
                  <li key={item.ticker}>
                    {item.ticker}: {item.survivors} survivors
                    {item.limited_history ? " · not required" : ""}
                  </li>
                ))}
              </ul>
            </EmptyState>
          ) : (
            <DataTable caption="Rules that qualified across tickers" data={cross.data ?? []} columns={crossColumns} />
          )}
        </TabsContent>
      </Tabs>
    </div>
  )
}

function LeaderboardCards({
  items,
  state,
  runId,
  gates,
  onVariants,
  onSort,
  onToggleOrder,
}: {
  items: LeaderboardItem[]
  state: LeaderboardState
  runId: string
  gates: ReturnType<typeof gatesFromConfig> | undefined
  onVariants: (rule: string) => void
  onSort: (column: string) => void
  onToggleOrder: () => void
}) {
  if (items.length === 0) {
    return (
      <div className="md:hidden">
        <EmptyState title="No strategies match these filters." />
      </div>
    )
  }
  return (
    <div className="space-y-3 md:hidden">
      <div className="flex items-end gap-2">
        <label className="min-w-0 flex-1 text-xs text-muted-foreground">
          Sort
          <select
            aria-label="Sort"
            className="mt-1 min-h-9 w-full rounded-md border border-border bg-card px-2 py-1 text-sm text-foreground"
            value={state.sort}
            onChange={(event) => onSort(event.target.value)}
          >
            {LEADERBOARD_SORTS.map(([id, label]) => (
              <option key={id} value={id}>
                {id === "rank" ? rankColumnLabel(state.group) : label}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="inline-flex min-h-9 items-center rounded-md border border-border px-3 text-sm" onClick={onToggleOrder}>
          {state.order === "asc" ? "Ascending" : "Descending"}
        </button>
      </div>
      {items.map((item) => (
        <article key={`${item.strategy_id}-${item.ticker}`} className={cn("rounded-xl border border-border bg-card p-4", item.rejected && "bg-muted/80 text-muted-foreground")}>
          <p className="text-xs text-muted-foreground">
            {rankColumnLabel(state.group)} {item.rank ?? "—"} · {item.ticker}
          </p>
          <Link className="mt-1 inline-flex min-h-6 items-center font-medium text-foreground hover:underline" to={strategyHref(item, runId)} state={{ from: `/research/leaderboard?${serializeLeaderboardState(state, runId)}` }}>
            {item.strategy}
          </Link>
          <RuleLines entry={item.entry_signals} exit={item.exit_signals} filters={item.filter_signals} />
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <InfoHint label="Parameters" text={item.parameters || "No parameters"} />
            {(item.variants ?? 1) > 1 ? (
              <button type="button" className="inline-flex min-h-6 items-center text-xs text-foreground hover:underline" onClick={() => onVariants(item.strategy)}>
                +{(item.variants ?? 1) - 1} variants
              </button>
            ) : null}
          </div>
          {item.rejected ? <p className="mt-1 text-xs">{item.flags.map((flag) => flagLabel(flag, gates)).join(" · ")}</p> : null}
          <dl className="mt-3 grid grid-cols-2 gap-2 text-sm">
            <div>
              <dt className="text-xs text-muted-foreground">Robustness</dt>
              <dd className="num">{formatNum(item.robustness, 1)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Sharpe</dt>
              <dd className="num">{formatNum(item.sharpe)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Validation CAGR</dt>
              <dd className={`num ${tone(item.oos_cagr)}`}>{formatSignedPct(item.oos_cagr)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Max DD</dt>
              <dd className="num">{formatSignedPct(item.max_drawdown)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Win rate</dt>
              <dd className="num">{formatPct(item.win_rate)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Trades</dt>
              <dd className="num">{item.trades ?? "—"}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Holdout CAGR</dt>
              <dd className={`num ${tone(item.test_cagr)}`}>{formatSignedPct(item.test_cagr)}</dd>
            </div>
          </dl>
        </article>
      ))}
    </div>
  )
}

function leaderboardColumns(
  state: LeaderboardState,
  runId: string,
  gates: ReturnType<typeof gatesFromConfig> | undefined,
  onVariants: (rule: string) => void,
): LegacyColumnDef<LeaderboardItem>[] {
  const here = `${serializeLeaderboardState(state, runId)}`
  return [
    { accessorKey: "rank", header: rankColumnLabel(state.group), cell: ({ row }) => <span className="num-cell block">{row.original.rank ?? "—"}</span> },
    { accessorKey: "ticker", header: "Ticker" },
    {
      accessorKey: "strategy",
      header: "Rule",
      cell: ({ row }) => (
        <div className="max-w-md">
          <Link className="text-foreground hover:underline" to={strategyHref(row.original, runId)} state={{ from: `/research/leaderboard?${here}` }}>
            {row.original.strategy}
          </Link>
          <RuleLines entry={row.original.entry_signals} exit={row.original.exit_signals} filters={row.original.filter_signals} />
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <InfoHint label="Parameters" text={row.original.parameters || "No parameters"} />
            {(row.original.variants ?? 1) > 1 ? (
              <button type="button" className="inline-flex min-h-6 items-center text-xs text-foreground hover:underline" onClick={() => onVariants(row.original.strategy)}>
                +{(row.original.variants ?? 1) - 1} variants
              </button>
            ) : null}
          </div>
          {row.original.rejected ? (
            <p className="mt-1 text-xs">{row.original.flags.map((flag) => flagLabel(flag, gates)).join(" · ")}</p>
          ) : null}
        </div>
      ),
    },
    { accessorKey: "robustness", header: "Robustness", cell: ({ row }) => <span className="num-cell block">{formatNum(row.original.robustness, 1)}</span> },
    { accessorKey: "oos_cagr", header: "CAGR", cell: ({ row }) => <span className={`num-cell block ${tone(row.original.oos_cagr)}`}>{formatSignedPct(row.original.oos_cagr)}</span> },
    { accessorKey: "sharpe", header: "Sharpe", cell: ({ row }) => <span className="num-cell block">{formatNum(row.original.sharpe)}</span> },
    { accessorKey: "max_drawdown", header: "Max DD", cell: ({ row }) => <span className="num-cell block">{formatSignedPct(row.original.max_drawdown)}</span> },
    { accessorKey: "win_rate", header: "Win rate", cell: ({ row }) => <span className="num-cell block">{formatPct(row.original.win_rate)}</span> },
    { accessorKey: "trades", header: "Trades", cell: ({ row }) => <span className="num-cell block">{row.original.trades ?? "—"}</span> },
    { accessorKey: "test_cagr", header: "Holdout CAGR", cell: ({ row }) => <span className={`num-cell block ${tone(row.original.test_cagr)}`}>{formatSignedPct(row.original.test_cagr)}</span> },
  ]
}

function crossTickerColumns(tickers: string[], runId: string): LegacyColumnDef<CrossTickerItem>[] {
  return [
    {
      accessorKey: "strategy",
      header: "Rule",
      cell: ({ row }) => {
        const ticker = bestTicker(row.original.per_ticker)
        return (
          <div className="max-w-md">
            {ticker ? (
              <Link className="inline-flex min-h-6 items-center text-foreground hover:underline" to={`/research/strategies/${row.original.strategy_id}?ticker=${ticker}${runId ? `&run=${runId}` : ""}`}>
                {row.original.strategy}
              </Link>
            ) : (
              <span>{row.original.strategy}</span>
            )}
            {row.original.parameters ? <p className="mt-1 text-xs text-muted-foreground">{row.original.parameters}</p> : null}
          </div>
        )
      },
    },
    { accessorKey: "family", header: "Family", cell: ({ row }) => familyLabel(row.original.family) },
    { accessorKey: "cross_score", header: "Cross score", cell: ({ row }) => formatNum(row.original.cross_score, 1) },
    ...tickers.map(
      (ticker): LegacyColumnDef<CrossTickerItem> => ({
        id: ticker,
        header: ticker,
        cell: ({ row }) => {
          const stats = row.original.per_ticker[ticker]
          if (!stats) return "—"
          return (
            <span className={stats.rejected ? "text-muted-foreground" : undefined}>
              {formatNum(stats.score, 0)}
              {stats.limited ? " · short" : ""}
            </span>
          )
        },
      }),
    ),
  ]
}

function bestTicker(perTicker: CrossTickerItem["per_ticker"]): string {
  const ranked = Object.entries(perTicker).sort((left, right) => (right[1].score ?? -Infinity) - (left[1].score ?? -Infinity))
  return ranked[0]?.[0] ?? ""
}

function strategyHref(item: LeaderboardItem, runId: string): string {
  const params = new URLSearchParams({ ticker: item.ticker })
  if (runId) params.set("run", runId)
  return `/research/strategies/${item.strategy_id}?${params}`
}
