import { useQuery } from "@tanstack/react-query"
import { Link, useOutletContext } from "react-router-dom"
import { getJson } from "../api/client"
import type { OverviewCard, RunTicker } from "../api/types"
import { RuleLines, Score, WindowStats } from "../components/WindowStats"
import { Badge } from "../components/ui/badge"
import { Card, CardContent, CardHeader } from "../components/ui/card"
import { Alert } from "../components/ui/notice"
import { Skeleton } from "../components/ui/skeleton"
import { useDocumentTitle } from "../lib/useDocumentTitle"

type OutletContext = { runId: string }

export function Overview() {
  useDocumentTitle("Research overview · HyperOptions")
  const { runId } = useOutletContext<OutletContext>()
  const query = useQuery({
    queryKey: ["overview", runId],
    queryFn: () => getJson<OverviewCard[]>(`/api/research/overview${runId ? `?run_id=${runId}` : ""}`),
  })
  const coverage = useQuery({
    queryKey: ["run-tickers", runId],
    queryFn: () => getJson<RunTicker[]>(`/api/research/runs/${runId}/tickers`),
    enabled: Boolean(runId),
  })
  if (query.isLoading) {
    return (
      <div className="grid gap-4 md:grid-cols-2">
        <Skeleton className="h-64" />
        <Skeleton className="h-64" />
      </div>
    )
  }
  if (query.error) return <Alert>{query.error instanceof Error ? query.error.message : "Overview failed."}</Alert>
  const survivors = new Map((coverage.data ?? []).map((item) => [item.ticker, item.survivors]))
  return (
    <div>
      <h1 className="mb-2">Which rules hold up?</h1>
      <p className="mb-6 max-w-3xl text-muted-foreground">
        Train fits the rules, validation ranks them, and the holdout is shown afterwards. It is not part of the score.
      </p>
      <div className="grid gap-4 md:grid-cols-2">
        {(query.data ?? []).map((card) => (
          <Card key={card.ticker}>
            <CardHeader>
              <div>
                <h2 className="text-3xl">{card.ticker}</h2>
                <p className="text-sm text-muted-foreground">
                  {card.bars ? `${card.first} → ${card.last}` : "Update data to load history"}
                </p>
              </div>
              {card.limited_history ? <Badge>Limited history</Badge> : null}
            </CardHeader>
            <CardContent>
              {card.strategy_id ? (
                <>
                  <Score value={card.robustness} />
                  <p className="text-xs text-muted-foreground">Robustness</p>
                  <Link
                    className="mt-3 inline-block font-medium text-foreground hover:underline"
                    to={`/research/strategies/${card.strategy_id}?ticker=${card.ticker}${runId ? `&run=${runId}` : ""}`}
                    state={{ from: `/research${runId ? `?run=${runId}` : ""}` }}
                  >
                    {card.strategy_name}
                  </Link>
                  <RuleLines entry={card.entry_signals} exit={card.exit_signals} filters={card.filter_signals} />
                  <WindowStats
                    validation={{
                      cagr: card.oos_cagr,
                      buyHold: card.buy_hold_cagr,
                      drawdown: card.max_drawdown,
                      trades: card.trades,
                    }}
                    holdout={{
                      cagr: card.test_cagr,
                      buyHold: card.test_buy_hold_cagr,
                      drawdown: card.test_max_drawdown,
                      trades: card.test_trades,
                    }}
                  />
                  <Link
                    className="mt-4 inline-block text-sm text-foreground hover:underline"
                    to={leaderboardHref(card.ticker, runId, false)}
                  >
                    Compare rules for {card.ticker}
                  </Link>
                </>
              ) : (
                <div>
                  <p className="text-muted-foreground">No robust strategy yet.</p>
                  <p className="mt-2 text-sm text-muted-foreground">{survivors.get(card.ticker) ?? 0} survivors</p>
                  <Link className="mt-3 inline-block text-sm text-foreground hover:underline" to={leaderboardHref(card.ticker, runId, true)}>
                    See rejected candidates
                  </Link>
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}

function leaderboardHref(ticker: string, runId: string, rejected: boolean): string {
  const params = new URLSearchParams()
  params.set("ticker", ticker)
  if (runId) params.set("run", runId)
  if (rejected) params.set("rejected", "1")
  return `/research/leaderboard?${params.toString()}`
}
