// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import App from "./App"

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
