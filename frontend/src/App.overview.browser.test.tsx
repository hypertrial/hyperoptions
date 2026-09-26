// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import App from "./App"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.sessionStorage.clear()
})

it("keeps the research overview as a strategy detail return path", async () => {
  window.history.replaceState(null, "", "/research?run=run-1")
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes("/strategies/")) return new Response('{"detail":"not found"}', { status: 404 })
    const body = url.includes("/overview") ? [{
      ticker: "IREN", bars: 1000, first: "2020-01-01", last: "2025-01-01",
      limited_history: false, strategy_id: "rule-1", strategy_name: "Test rule",
    }] : []
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })
  }))

  render(<App />)
  const strategy = await screen.findByRole("link", { name: "Test rule" })
  fireEvent.click(strategy)

  await waitFor(() => expect(window.location.pathname).toBe("/research/strategies/rule-1"))
  expect(window.history.state.usr.from).toBe("/research?run=run-1")
})
