import type { Run } from "../api/types"

export function runLabel(run: Run, latest: boolean): string {
  const when = new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(run.created_at))
  const count = run.strategy_count.toLocaleString("en-US")
  const latestMark = latest ? " · latest" : ""
  return `${when} · ${count} strategies · ${run.ticker_count} tickers${latestMark}`
}

export function dataThrough(lastDates: (string | null | undefined)[]): string {
  const dates = lastDates.filter((value): value is string => Boolean(value)).sort()
  const latest = dates.at(-1)
  return latest ? `Data through ${latest}` : "No market data"
}
