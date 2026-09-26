// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import App from "./App"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.sessionStorage.clear()
})

it.each([
  "/research",
  "/research/leaderboard?run=old",
  "/research/strategies/rule-1?ticker=IREN",
])("routes retired Research link %s to the watchlist", async (path) => {
  window.history.replaceState(null, "", path)
  const fetchMock = vi.fn(async (_input: RequestInfo | URL) => new Response(JSON.stringify({ items: [] }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))
  vi.stubGlobal("fetch", fetchMock)

  render(<App />)

  expect(await screen.findByRole("heading", { name: "Watchlist" })).toBeTruthy()
  await waitFor(() => expect(window.location.pathname).toBe("/watchlist"))
  expect(screen.getByRole("link", { name: "Watchlist" }).getAttribute("aria-current")).toBe("page")
  expect(fetchMock).toHaveBeenCalledWith("/api/watchlist", expect.anything())
  expect(fetchMock.mock.calls.every(([url]) => !String(url).startsWith("/api/research/"))).toBe(true)
})
