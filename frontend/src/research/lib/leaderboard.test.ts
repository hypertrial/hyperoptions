import { describe, expect, it } from "vitest"
import {
  LEADERBOARD_PAGE_SIZE,
  initialLeaderboardState,
  jobPollInterval,
  leaderboardQuery,
  applySort,
  clearLeaderboardFilters,
  leaderboardSortable,
  pageBounds,
  rankColumnLabel,
  pageStateForRun,
  parseLeaderboardState,
  resetPage,
  serializeLeaderboardState,
  toggleSort,
  withFilter,
} from "./leaderboard"

describe("leaderboard query state", () => {
  it("builds the server filter and sort query", () => {
    const query = leaderboardQuery(
      {
        ...initialLeaderboardState,
        ticker: "IREN",
        family: "trend_momentum",
        exit: "atr_trail",
        filters: "2",
        minTrades: "20",
        includeRejected: true,
        sort: "sharpe",
        order: "asc",
      },
      "run-1",
    )
    const params = new URLSearchParams(query)
    expect(params.get("run_id")).toBe("run-1")
    expect(params.get("ticker")).toBe("IREN")
    expect(params.get("family")).toBe("trend_momentum")
    expect(params.get("exit_kind")).toBe("atr_trail")
    expect(params.get("filters")).toBe("2")
    expect(params.get("min_trades")).toBe("20")
    expect(params.get("include_rejected")).toBe("true")
    expect(params.get("sort")).toBe("sharpe")
    expect(params.get("order")).toBe("asc")
    expect(params.get("limit")).toBe(String(LEADERBOARD_PAGE_SIZE))
    expect(params.get("offset")).toBe("0")
    expect(params.get("group_variants")).toBe("true")
    expect(params.get("sort")).not.toBe("test_cagr")
  })

  it("resets the page when a filter or sort changes", () => {
    const paged = { ...initialLeaderboardState, offset: 100 }
    expect(withFilter(paged, { ticker: "IREN" }).offset).toBe(0)
    expect(toggleSort(paged, "sharpe").offset).toBe(0)
    expect(toggleSort(paged, "test_cagr").offset).toBe(100)
    const second = leaderboardQuery({ ...initialLeaderboardState, offset: 100 }, "run-1")
    expect(new URLSearchParams(second).get("offset")).toBe("100")
  })

  it("reports pager bounds", () => {
    expect(pageBounds(0, 272)).toEqual({ from: 1, to: 100, hasPrev: false, hasNext: true })
    expect(pageBounds(100, 272)).toEqual({ from: 101, to: 200, hasPrev: true, hasNext: true })
    expect(pageBounds(200, 272)).toEqual({ from: 201, to: 272, hasPrev: true, hasNext: false })
    expect(pageBounds(0, 0)).toEqual({ from: 0, to: 0, hasPrev: false, hasNext: false })
    expect(pageBounds(200, 79)).toEqual({ from: 0, to: 0, hasPrev: true, hasNext: false })
    const paged = { ...initialLeaderboardState, offset: 100, ticker: "WULF" }
    expect(resetPage(paged)).toMatchObject({ offset: 0, ticker: "WULF" })
    expect(resetPage(initialLeaderboardState)).toBe(initialLeaderboardState)
    const aligned = pageStateForRun(paged, "run-b", "run-a")
    expect(new URLSearchParams(leaderboardQuery(aligned, "run-b")).get("offset")).toBe("0")
    expect(aligned.ticker).toBe("WULF")
    expect(pageStateForRun(paged, "run-a", "run-a").offset).toBe(100)
  })

  it("toggles the active sort and defaults new metrics to descending", () => {
    const flipped = toggleSort(initialLeaderboardState, "robustness")
    expect(flipped.order).toBe("asc")
    const sharpe = toggleSort(initialLeaderboardState, "sharpe")
    expect(sharpe).toMatchObject({ sort: "sharpe", order: "desc" })
    expect(toggleSort(initialLeaderboardState, "ticker").order).toBe("asc")
    expect(toggleSort(initialLeaderboardState, "signals")).toMatchObject({ sort: "signals", order: "asc" })
    expect(toggleSort(initialLeaderboardState, "parameters")).toMatchObject({
      sort: "parameters",
      order: "asc",
    })
    expect(leaderboardSortable("test_cagr")).toBe(false)
    expect(rankColumnLabel(true)).toBe("Best rank")
    expect(rankColumnLabel(false)).toBe("Rank")
    expect(applySort(initialLeaderboardState, "sharpe")).toMatchObject({ sort: "sharpe", order: "desc", offset: 0 })
    const paged = { ...initialLeaderboardState, offset: 80 }
    expect(applySort(paged, "robustness")).toBe(paged)
    expect(applySort(paged, "test_cagr")).toBe(paged)
    expect(toggleSort(initialLeaderboardState, "test_cagr")).toEqual(initialLeaderboardState)
  })
})

describe("leaderboard url state", () => {
  it("round-trips a full query and treats a missing group as on", () => {
    const state = {
      ...initialLeaderboardState,
      ticker: "IREN",
      family: "trend_momentum",
      exit: "atr_trail",
      filters: "2",
      minTrades: "20",
      includeRejected: true,
      group: false,
      rule: "Supertrend + ROC",
      sort: "sharpe",
      order: "asc" as const,
      offset: 200,
      view: "cross" as const,
    }
    const parsed = parseLeaderboardState(serializeLeaderboardState(state, "run-1"))
    expect(parsed).toEqual(state)
    const defaults = parseLeaderboardState(new URLSearchParams("run=run-1"))
    expect(defaults.group).toBe(true)
    expect(defaults.offset).toBe(0)
    expect(serializeLeaderboardState(defaults, "run-1").get("page")).toBeNull()
  })

  it("drops invalid sort, order, view, page, and min trades", () => {
    const parsed = parseLeaderboardState(
      new URLSearchParams("sort=test_cagr&order=sideways&view=nope&page=0&min_trades=12abc"),
    )
    expect(parsed.sort).toBe("robustness")
    expect(parsed.order).toBe("desc")
    expect(parsed.view).toBe("leaderboard")
    expect(parsed.offset).toBe(0)
    expect(parsed.minTrades).toBe("")
    expect(parsed.exit).toBe("")
    expect(parsed.filters).toBe("")
    expect(leaderboardQuery(parsed, "run-1")).not.toContain("sort=test_cagr")
    const ignored = parseLeaderboardState(new URLSearchParams("exit=stop&filters=9"))
    expect(ignored.exit).toBe("")
    expect(ignored.filters).toBe("")
    const cleared = clearLeaderboardFilters({
      ...initialLeaderboardState,
      exit: "time",
      filters: "1",
      ticker: "IREN",
    })
    expect(cleared.exit).toBe("")
    expect(cleared.filters).toBe("")
    expect(serializeLeaderboardState(cleared, "run-1").get("exit")).toBeNull()
  })

  it("resets the page when the run changes before the next request", () => {
    const paged = { ...initialLeaderboardState, offset: 100, ticker: "CIFR" }
    const reset = pageStateForRun(paged, "next", "current")
    expect(reset.offset).toBe(0)
    expect(new URLSearchParams(leaderboardQuery(reset, "next")).get("offset")).toBe("0")
  })
})

describe("job polling", () => {
  it("polls only while a job is unfinished", () => {
    expect(jobPollInterval("running")).toBe(1000)
    expect(jobPollInterval("queued")).toBe(1000)
    expect(jobPollInterval("succeeded")).toBe(false)
    expect(jobPollInterval(undefined)).toBe(false)
  })
})
