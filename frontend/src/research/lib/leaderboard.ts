export type SortOrder = "asc" | "desc"
export type LeaderboardView = "leaderboard" | "cross"

export const LEADERBOARD_PAGE_SIZE = 100

const SORTS = new Set([
  "rank",
  "ticker",
  "strategy",
  "signals",
  "parameters",
  "robustness",
  "oos_cagr",
  "sharpe",
  "max_drawdown",
  "win_rate",
  "trades",
])

const EXITS = new Set(["mirror", "atr_trail", "time"])
const FILTER_COUNTS = new Set(["0", "1", "2"])

export type LeaderboardState = {
  ticker: string
  family: string
  exit: string
  filters: string
  minTrades: string
  includeRejected: boolean
  group: boolean
  rule: string
  sort: string
  order: SortOrder
  offset: number
  view: LeaderboardView
}

export const initialLeaderboardState: LeaderboardState = {
  ticker: "",
  family: "",
  exit: "",
  filters: "",
  minTrades: "",
  includeRejected: false,
  group: true,
  rule: "",
  sort: "robustness",
  order: "desc",
  offset: 0,
  view: "leaderboard",
}

export function resetPage(state: LeaderboardState): LeaderboardState {
  return state.offset === 0 ? state : { ...state, offset: 0 }
}

export function pageStateForRun(
  state: LeaderboardState,
  runId: string,
  appliedRunId: string,
): LeaderboardState {
  return runId === appliedRunId ? state : resetPage(state)
}

export function withFilter(
  state: LeaderboardState,
  patch: Partial<Omit<LeaderboardState, "offset">>,
): LeaderboardState {
  return { ...state, ...patch, offset: 0 }
}

export function parseLeaderboardState(params: URLSearchParams): LeaderboardState {
  const sort = params.get("sort") ?? ""
  const order = params.get("order")
  const view = params.get("view")
  const page = Number(params.get("page"))
  const minTrades = params.get("min_trades") ?? ""
  return {
    ticker: params.get("ticker") ?? "",
    family: params.get("family") ?? "",
    exit: EXITS.has(params.get("exit") ?? "") ? (params.get("exit") ?? "") : "",
    filters: FILTER_COUNTS.has(params.get("filters") ?? "") ? (params.get("filters") ?? "") : "",
    minTrades: /^\d+$/.test(minTrades) ? minTrades : "",
    includeRejected: params.get("rejected") === "1",
    group: params.get("group") !== "0",
    rule: params.get("rule") ?? "",
    sort: SORTS.has(sort) ? sort : "robustness",
    order: order === "asc" || order === "desc" ? order : "desc",
    offset: Number.isInteger(page) && page > 1 ? (page - 1) * LEADERBOARD_PAGE_SIZE : 0,
    view: view === "cross" ? "cross" : "leaderboard",
  }
}

export function serializeLeaderboardState(state: LeaderboardState, runId: string): URLSearchParams {
  const params = new URLSearchParams()
  if (runId) params.set("run", runId)
  if (state.ticker) params.set("ticker", state.ticker)
  if (state.family) params.set("family", state.family)
  if (state.exit) params.set("exit", state.exit)
  if (state.filters) params.set("filters", state.filters)
  if (state.minTrades) params.set("min_trades", state.minTrades)
  if (state.includeRejected) params.set("rejected", "1")
  if (!state.group) params.set("group", "0")
  if (state.rule) params.set("rule", state.rule)
  if (state.sort !== "robustness") params.set("sort", state.sort)
  if (state.order !== "desc") params.set("order", state.order)
  const page = Math.floor(state.offset / LEADERBOARD_PAGE_SIZE) + 1
  if (page > 1) params.set("page", String(page))
  if (state.view === "cross") params.set("view", "cross")
  return params
}

export function leaderboardQuery(state: LeaderboardState, runId: string | undefined): string {
  const params = new URLSearchParams()
  if (runId) params.set("run_id", runId)
  if (state.ticker) params.set("ticker", state.ticker)
  if (state.family) params.set("family", state.family)
  if (state.exit) params.set("exit_kind", state.exit)
  if (state.filters) params.set("filters", state.filters)
  if (state.minTrades) params.set("min_trades", state.minTrades)
  if (state.includeRejected) params.set("include_rejected", "true")
  if (state.group) params.set("group_variants", "true")
  if (state.rule) params.set("rule", state.rule)
  params.set("sort", state.sort)
  params.set("order", state.order)
  params.set("limit", String(LEADERBOARD_PAGE_SIZE))
  params.set("offset", String(state.offset))
  return params.toString()
}

export function leaderboardSortable(column: string): boolean {
  return column !== "test_cagr" && SORTS.has(column)
}

export const LEADERBOARD_SORTS = [
  ["rank", "Rank"],
  ["ticker", "Ticker"],
  ["strategy", "Rule"],
  ["robustness", "Robustness"],
  ["oos_cagr", "CAGR"],
  ["sharpe", "Sharpe"],
  ["max_drawdown", "Max DD"],
  ["win_rate", "Win rate"],
  ["trades", "Trades"],
] as const

export function rankColumnLabel(grouped: boolean): string {
  return grouped ? "Best rank" : "Rank"
}

export function sortOrderFor(column: string): SortOrder {
  const textual = column === "ticker" || column === "strategy" || column === "signals" || column === "parameters"
  return textual ? "asc" : "desc"
}

export function applySort(state: LeaderboardState, column: string): LeaderboardState {
  if (!leaderboardSortable(column) || state.sort === column) return state
  return { ...state, sort: column, order: sortOrderFor(column), offset: 0 }
}

export function toggleSort(state: LeaderboardState, column: string): LeaderboardState {
  if (!leaderboardSortable(column)) return state
  if (state.sort === column) {
    return { ...state, order: state.order === "desc" ? "asc" : "desc", offset: 0 }
  }
  return { ...state, sort: column, order: sortOrderFor(column), offset: 0 }
}

export function pageBounds(offset: number, total: number, pageSize = LEADERBOARD_PAGE_SIZE) {
  if (total <= 0 || offset >= total) {
    return { from: 0, to: 0, hasPrev: offset > 0, hasNext: false }
  }
  const from = offset + 1
  const to = Math.min(offset + pageSize, total)
  return { from, to, hasPrev: offset > 0, hasNext: offset + pageSize < total }
}

export function jobPollInterval(state: string | undefined): number | false {
  if (state === "queued" || state === "running") return 1000
  return false
}

export function clearLeaderboardFilters(state: LeaderboardState): LeaderboardState {
  return {
    ...state,
    ticker: "",
    family: "",
    exit: "",
    filters: "",
    minTrades: "",
    includeRejected: false,
    rule: "",
    offset: 0,
  }
}
