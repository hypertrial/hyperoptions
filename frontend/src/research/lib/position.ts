export type SegmentBound = { start: string | null; end: string | null }

export function currentPosition(
  entryMarks: (number | null)[],
  exitMarks: (number | null)[],
  dates: string[],
): { side: "long" | "flat"; date: string | null } {
  const length = Math.min(entryMarks.length, exitMarks.length, dates.length)
  let last: { side: "long" | "flat"; date: string } | null = null
  for (let index = 0; index < length; index += 1) {
    if (entryMarks[index] != null) last = { side: "long", date: dates[index] }
    if (exitMarks[index] != null) last = { side: "flat", date: dates[index] }
  }
  return last ?? { side: "flat", date: null }
}

export function tradeSegment(exitDate: string, bounds: Record<string, SegmentBound>): string {
  for (const name of ["train", "validation", "test"]) {
    const bound = bounds[name]
    if (!bound?.start || !bound.end) continue
    if (exitDate >= bound.start && exitDate <= bound.end) return name
  }
  return ""
}

export function safeReturnPath(value: unknown, ticker: string, runId: string): string {
  if (typeof value === "string" && (value.startsWith("/research/leaderboard") || value === "/research" || value.startsWith("/research?"))) {
    return value
  }
  const params = new URLSearchParams()
  if (ticker) params.set("ticker", ticker)
  if (runId) params.set("run", runId)
  const query = params.toString()
  return query ? `/research/leaderboard?${query}` : "/research/leaderboard"
}
