// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import App from "./App"

vi.mock("./research/charts/PlotlyChart", () => ({ PlotlyChart: () => null }))

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.sessionStorage.clear()
})

it("opens a research deep link and requests namespaced data", async () => {
  window.history.replaceState(null, "", "/research/leaderboard")
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    const body = path.endsWith("/config")
      ? { tickers: [], gates: {} }
      : []
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })
  })
  vi.stubGlobal("fetch", fetchMock)

  render(<App />)

  expect(await screen.findByRole("heading", { name: "Strategy leaderboard" })).toBeTruthy()
  expect(screen.getByRole("link", { name: "Option chain" }).getAttribute("href")).toBe("/")
  expect(screen.getByRole("link", { name: "Research" }).getAttribute("aria-current")).toBe("page")
  await waitFor(() => expect(fetchMock).toHaveBeenCalled())
  expect(fetchMock.mock.calls.every(([path]) => String(path).startsWith("/api/research/"))).toBe(true)
})

it("uses the selected run tickers for leaderboard filters and cross-ticker columns", async () => {
  window.history.replaceState(null, "", "/research/leaderboard?run=custom")
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    const body = path.includes("/runs/custom/tickers")
      ? [{ ticker: "AAPL", survivors: 1, limited_history: false }]
      : path.endsWith("/config")
        ? { tickers: ["IREN"], gates: {} }
        : path.endsWith("/runs")
          ? [{ id: "custom", created_at: "2026-01-01T00:00:00Z", status: "completed", strategy_count: 1, ticker_count: 1 }]
          : path.includes("/leaderboard?")
            ? { run_id: "custom", total: 0, families: [], items: [] }
            : path.includes("/cross-ticker?")
              ? [{ strategy_id: "rule-1", strategy: "Test rule", family: "trend_momentum", signals: "test", parameters: "", cross_score: 1, per_ticker: { AAPL: { score: 1, rejected: false, sharpe: 1, limited: false } } }]
              : []
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })
  }))

  render(<App />)

  const ticker = await screen.findByRole("combobox", { name: "Ticker" })
  await waitFor(() => expect(ticker.textContent).toContain("AAPL"))
  expect(ticker.textContent).not.toContain("IREN")
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Cross-Ticker Strategies" }), { button: 0 })
  await waitFor(() => expect(screen.getByRole("tab", { name: "Cross-Ticker Strategies" }).getAttribute("aria-selected")).toBe("true"))
  expect(await screen.findByRole("columnheader", { name: "AAPL" })).toBeTruthy()
  expect(screen.queryByRole("columnheader", { name: "IREN" })).toBeNull()
})

it("uses selected run tickers in strategy detail navigation", async () => {
  window.history.replaceState(null, "", "/research/strategies/rule-1?ticker=AAPL&run=custom")
  let releaseCoverage: (() => void) | undefined
  const coveragePending = new Promise<void>((resolve) => { releaseCoverage = resolve })
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path.includes("/runs/custom/tickers")) await coveragePending
    const body = path.includes("/runs/custom/tickers")
      ? [{ ticker: "AAPL" }, { ticker: "MSFT" }]
      : path.endsWith("/config")
        ? { tickers: ["IREN"], gates: { min_degradation: 0.77 } }
        : path.includes("/strategies/rule-1?")
          ? {
              ticker: "AAPL", name: "Test rule", family: "trend_momentum", signals: "test",
              parameters: {}, entry_signals: [], filter_signals: [], exit_signals: [],
              dates: [], close: [], entry_marks: [], exit_marks: [], equity: [], buy_hold: [], drawdown: [],
              segments: [], segment_bounds: {}, reopt: [], folds: [], trades: [], flags: [],
            }
          : []
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })
  }))

  render(<App />)

  const ticker = await screen.findByRole("combobox", { name: "Ticker" })
  await screen.findByText("Gate: at least 0.77 of train Sharpe.")
  expect(ticker.textContent).toBe("AAPL")
  releaseCoverage?.()
  await waitFor(() => expect(ticker.textContent).toContain("MSFT"))
  expect(ticker.textContent).toContain("AAPL")
  expect(ticker.textContent).not.toContain("IREN")
})
