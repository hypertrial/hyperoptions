// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import App from "./App"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.sessionStorage.clear()
})

it("keeps the live chain query on the option chain link", () => {
  window.history.replaceState(null, "", "/?t=CIFR&side=put&m=otm")
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })))

  render(<App />)

  const href = screen.getByRole("link", { name: "Option chain" }).getAttribute("href")
  expect(href).toContain("t=CIFR")
  expect(href).toContain("side=put")
  expect(href).toContain("m=otm")
})

it("restores that chain query after opening the watchlist", async () => {
  window.history.replaceState(null, "", "/?t=CIFR&side=put&m=otm")
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [] }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })))

  render(<App />)
  fireEvent.click(screen.getByRole("link", { name: "Watchlist" }))
  expect(await screen.findByRole("heading", { name: "Watchlist" })).toBeTruthy()
  fireEvent.click(screen.getByRole("link", { name: "Option chain" }))
  await waitFor(() => expect(window.location.search).toContain("t=CIFR"))
  expect(window.location.search).toContain("side=put")
  expect(window.location.search).toContain("m=otm")
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
