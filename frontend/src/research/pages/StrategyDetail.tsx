import { useQuery } from "@tanstack/react-query"
import { useMemo, useState } from "react"
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom"
import { getJson } from "../api/client"
import type { ConfigView, StrategyDetail as Detail } from "../api/types"
import { performanceFigure, segmentSwatch } from "../charts/performance"
import { PlotlyChart } from "../charts/PlotlyChart"
import { RuleLines, Score, WindowStats } from "../components/WindowStats"
import { Badge } from "../components/ui/badge"
import { Card, CardContent, CardHeader } from "../components/ui/card"
import { Alert } from "../components/ui/notice"
import { Skeleton } from "../components/ui/skeleton"
import { formatInt, formatNum, formatPct, formatSignedPct, tone } from "../lib/format"
import { METRICS, exitLabel, familyLabel, flagLabel, gatesFromConfig, segmentLabel } from "../lib/labels"
import { currentPosition, safeReturnPath, tradeSegment } from "../lib/position"
import { useDocumentTitle } from "../lib/useDocumentTitle"
import { useTheme } from "../../theme"

const SEGMENT_ORDER = ["train", "validation", "test", "full"]

export function StrategyDetail() {
  const { strategyId = "" } = useParams()
  const location = useLocation()
  const [params, setParams] = useSearchParams()
  const [showTrades, setShowTrades] = useState(false)
  const { dark } = useTheme()
  const ticker = params.get("ticker") ?? ""
  const runId = params.get("run") ?? ""
  const config = useQuery({
    queryKey: ["config"],
    queryFn: () => getJson<ConfigView>("/api/research/config"),
  })
  const query = useQuery({
    queryKey: ["strategy", strategyId, ticker, runId],
    queryFn: () => getJson<Detail>(`/api/research/strategies/${strategyId}?ticker=${ticker}${runId ? `&run_id=${runId}` : ""}`),
    enabled: Boolean(strategyId && ticker),
  })
  const detail = query.data
  useDocumentTitle(detail ? `${detail.name} · ${detail.ticker} · HyperOptions` : "Research strategy · HyperOptions")
  const figure = useMemo(() => (detail ? performanceFigure(detail, dark) : null), [detail, dark])
  const swatches = segmentSwatch(dark)
  if (!ticker) {
    return (
      <p className="text-muted-foreground">
        Choose a ticker from the{" "}
        <Link className="text-foreground underline" to={runId ? `/research/leaderboard?run=${runId}` : "/research/leaderboard"}>
          leaderboard
        </Link>
        .
      </p>
    )
  }
  if (query.isLoading || query.status === "pending") return <Skeleton className="h-96" />
  if (query.error || !detail || !figure) {
    return <Alert>{errorMessage(query.error)}</Alert>
  }

  const position = currentPosition(detail.entry_marks, detail.exit_marks, detail.dates)
  const validation = segment(detail, "validation")
  const holdout = segment(detail, "test")
  const ordered = [...detail.segments].sort(
    (left, right) => SEGMENT_ORDER.indexOf(left.segment) - SEGMENT_ORDER.indexOf(right.segment),
  )
  const reopt = new Map(detail.reopt.map((fold) => [fold.fold, fold]))
  const trades = showTrades ? detail.trades : detail.trades.slice(0, 20)
  const wins = detail.trades.filter((trade) => trade.return > 0).length
  const gates = config.data ? gatesFromConfig(config.data.gates) : undefined
  const back = safeReturnPath((location.state as { from?: unknown } | null)?.from, ticker, runId)

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Link className="inline-flex min-h-6 items-center text-sm text-foreground hover:underline" to={back}>
          Back
        </Link>
        <label className="text-sm text-muted-foreground">
          Ticker
          <select
            className="ml-2 min-h-9 rounded-md border border-border bg-card px-2 py-1 text-foreground"
            value={ticker}
            aria-label="Ticker"
            onChange={(event) => {
              const next = new URLSearchParams(params)
              next.set("ticker", event.target.value)
              setParams(next)
            }}
          >
            {(config.data?.tickers ?? [ticker]).map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
      </div>
      <div>
        <p className="text-sm text-muted-foreground">{detail.ticker}</p>
        <h1>{detail.name}</h1>
        <div className="mt-2 flex flex-wrap items-end gap-4">
          <div>
            <Score value={detail.robustness} />
            <p className="text-xs text-muted-foreground">{METRICS.robustness.label}</p>
          </div>
          <Badge>{familyLabel(detail.family)}</Badge>
          <Badge>{exitLabel(detail.exit_kind ?? "unknown")}</Badge>
          {detail.rank != null ? <Badge>Rank {detail.rank}</Badge> : null}
          {detail.data_snapshot === "changed" ? <Badge>Bars changed since this run</Badge> : null}
          {detail.rejected ? <Badge>Rejected</Badge> : null}
        </div>
        <p className="mt-3 text-sm">
          {position.side === "long" ? "Long" : "Flat"} as of {detail.dates.at(-1) ?? "the last bar"}
        </p>
        <WindowStats
          validation={{
            cagr: validation?.cagr ?? null,
            buyHold: validation?.buy_hold_cagr ?? null,
            drawdown: validation?.max_drawdown ?? null,
            trades: validation?.n_trades ?? null,
          }}
          holdout={{
            cagr: holdout?.cagr ?? null,
            buyHold: holdout?.buy_hold_cagr ?? null,
            drawdown: holdout?.max_drawdown ?? null,
            trades: holdout?.n_trades ?? null,
          }}
        />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <h2>Rules</h2>
          </CardHeader>
          <CardContent>
            <RuleLines entry={detail.entry_signals} exit={detail.exit_signals} filters={detail.filter_signals} />
            <details className="mt-3 text-sm">
              <summary className="inline-flex min-h-6 cursor-pointer items-center text-muted-foreground">Parameters</summary>
              <p className="mt-2 text-xs text-muted-foreground">
                {Object.entries(detail.parameters)
                  .map(([key, value]) => `${key}=${value}`)
                  .join(", ") || "None"}
              </p>
            </details>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <h2>Segments</h2>
          </CardHeader>
          <CardContent>
            <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <caption className="sr-only">Train, validation, holdout, and full-sample results</caption>
              <thead className="text-left text-xs text-muted-foreground">
                <tr>
                  <th scope="col">Segment</th>
                  <th scope="col" className="num-cell">CAGR</th>
                  <th scope="col" className="num-cell">Buy & hold</th>
                  <th scope="col" className="num-cell">Sharpe</th>
                  <th scope="col" className="num-cell">Max DD</th>
                  <th scope="col" className="num-cell">Trades</th>
                </tr>
              </thead>
              <tbody>
                {ordered.map((row) => (
                  <tr key={row.segment} className="border-t border-border">
                    <th scope="row" className="py-1 text-left font-normal">
                      {segmentLabel(row.segment)}
                      {row.segment === "test" ? <span className="text-xs text-muted-foreground"> · not ranked</span> : null}
                    </th>
                    <td className={`num-cell ${tone(row.cagr)}`}>{formatSignedPct(row.cagr)}</td>
                    <td className={`num-cell ${tone(row.buy_hold_cagr)}`}>{formatSignedPct(row.buy_hold_cagr)}</td>
                    <td className="num-cell">{formatNum(row.sharpe)}</td>
                    <td className="num-cell">{formatSignedPct(row.max_drawdown)}</td>
                    <td className="num-cell">{formatInt(row.n_trades)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          </CardContent>
        </Card>
      </div>
      <Card>
        <CardHeader>
          <h2>Score factors</h2>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-3 text-sm sm:grid-cols-3">
            <Factor
              label="Degradation"
              value={formatNum(detail.degradation)}
              hint={`Gate: at least ${gates?.minDegradation ?? 0.4} of train Sharpe.`}
            />
            <Factor
              label="Stability"
              value={formatNum(detail.stability)}
              hint={`Gate: at least ${gates?.minStability ?? 0.5}.`}
            />
            <Factor label="Walk-forward" value={formatNum(detail.walk_forward_consistency)} hint="Share of positive out-of-sample fold Sharpes, mixed with how steady they are." />
          </dl>
          {detail.flags.length ? (
            <ul className="mt-3 space-y-1 text-sm">
              {detail.flags.map((flag) => (
                <li key={flag}>{flagLabel(flag, gates)}</li>
              ))}
            </ul>
          ) : (
            <p className="mt-3 text-sm text-muted-foreground">No rejection flags.</p>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <h2>Performance</h2>
        </CardHeader>
        <CardContent>
          <p className="mb-2 text-xs text-muted-foreground">Markers are the fill bars. Orders fill at the next bar open. Up triangles are entries and down triangles are exits.</p>
          <ul className="mb-3 flex flex-wrap gap-3 text-xs text-muted-foreground">
            <Swatch color={swatches.train} label="Train" />
            <Swatch color={swatches.validation} label="Validation" />
            <Swatch color={swatches.test} label="Holdout" />
          </ul>
          <PlotlyChart data={figure.data} layout={figure.layout} className="h-[36rem] w-full md:h-[45rem]" />
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <h2>Walk-forward</h2>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
          <table className="w-full min-w-[36rem] text-sm">
            <caption className="sr-only">Walk-forward folds before the holdout</caption>
            <thead className="text-left text-xs text-muted-foreground">
              <tr>
                <th scope="col">Fold</th>
                <th scope="col" className="num-cell">IS Sharpe</th>
                <th scope="col" className="num-cell">OOS Sharpe</th>
                <th scope="col" className="num-cell">OOS return</th>
                <th scope="col" className="num-cell">Re-optimized OOS Sharpe</th>
              </tr>
            </thead>
            <tbody>
              {detail.folds.map((fold) => (
                <tr key={fold.fold} className="border-t border-border">
                  <th scope="row" className="py-1 text-left font-normal">{fold.fold}</th>
                  <td className="num-cell">{formatNum(fold.is_sharpe)}</td>
                  <td className="num-cell">{formatNum(fold.oos_sharpe)}</td>
                  <td className="num-cell">{formatPct(fold.oos_return)}</td>
                  <td className="num-cell">{formatNum(reopt.get(fold.fold)?.oos_sharpe)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <h2>Trades</h2>
        </CardHeader>
        <CardContent>
          <p className="mb-3 text-sm text-muted-foreground">
            {detail.trades.length} closed trades · {detail.trades.length ? formatPct(wins / detail.trades.length) : "—"} winners
          </p>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[40rem] text-sm">
              <caption className="sr-only">Closed trades tagged by the segment that contains the exit</caption>
              <thead className="text-left text-xs text-muted-foreground">
                <tr>
                  <th scope="col">Entry</th>
                  <th scope="col">Exit</th>
                  <th scope="col">Segment</th>
                  <th scope="col" className="num-cell">Entry price</th>
                  <th scope="col" className="num-cell">Exit price</th>
                  <th scope="col" className="num-cell">Return</th>
                  <th scope="col" className="num-cell">Bars</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((trade) => (
                  <tr key={`${trade.entry_date}-${trade.exit_date}`} className="border-t border-border">
                    <td className="py-1">{trade.entry_date}</td>
                    <td>{trade.exit_date}</td>
                    <td>{segmentLabel(tradeSegment(trade.exit_date, detail.segment_bounds ?? {})) || "—"}</td>
                    <td className="num-cell">{formatNum(trade.entry_price)}</td>
                    <td className="num-cell">{formatNum(trade.exit_price)}</td>
                    <td className={`num-cell ${tone(trade.return)}`}>{formatSignedPct(trade.return)}</td>
                    <td className="num-cell">{trade.holding_bars}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {detail.trades.length > 20 ? (
            <button type="button" className="mt-3 inline-flex min-h-6 items-center text-sm text-foreground hover:underline" onClick={() => setShowTrades((current) => !current)}>
              {showTrades ? "Show first 20" : `Show all ${detail.trades.length}`}
            </button>
          ) : null}
        </CardContent>
      </Card>
    </div>
  )
}

function segment(detail: Detail, name: string) {
  return detail.segments.find((row) => row.segment === name)
}

function Factor({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="num">{value}</dd>
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  )
}

function Swatch({ color, label }: { color: string; label: string }) {
  return (
    <li className="inline-flex min-h-6 items-center gap-1.5">
      <span className="inline-block size-3 rounded-sm" style={{ backgroundColor: color }} />
      {label}
    </li>
  )
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message
  return "Strategy not found."
}
