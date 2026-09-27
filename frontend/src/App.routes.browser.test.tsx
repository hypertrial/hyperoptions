// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"

import App from "./App"
import { samplePage } from "./testFixtures"

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.sessionStorage.clear()
  window.localStorage.clear()
})

it("keeps the live chain query on the option chain link", () => {
  window.history.replaceState(null, "", "/?t=CIFR&side=put&m=otm")
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })))

  render(<App />)

  const href = screen.getByRole("link", { name: "Option chain" }).getAttribute("href")
  expect(href).toContain("t=CIFR")
  expect(href).toContain("side=put")
  expect(href).toContain("m=otm")
  expect(document.title).toBe("Option chain · HyperOptions")
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
  expect(document.title).toBe("Watchlist · HyperOptions")
  fireEvent.click(screen.getByRole("link", { name: "Option chain" }))
  await waitFor(() => expect(window.location.search).toContain("t=CIFR"))
  expect(window.location.search).toContain("side=put")
  expect(window.location.search).toContain("m=otm")
  expect(document.title).toBe("Option chain · HyperOptions")
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

it("offers navigation when a route does not exist", () => {
  window.history.replaceState(null, "", "/missing-page")
  render(<App />)

  const main = screen.getByRole("main")
  expect(main.textContent).toContain("Page not found")
  expect(main.querySelector('a[href="/"]')?.textContent).toContain("option chain")
  expect(main.querySelector('a[href="/watchlist"]')?.textContent).toContain("watchlist")
  expect(document.title).toBe("Page not found · HyperOptions")
})

it("persists one model choice across chain and watchlist reads", async () => {
  window.history.replaceState(null, "", "/")
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(
    String(input).startsWith("/api/watchlist") ? { items: [] } : samplePage(),
  )))
  vi.stubGlobal("fetch", fetchMock)
  const mounted = render(<App />)
  const picker = screen.getByRole("combobox", { name: "Stock forecast model" }) as HTMLSelectElement
  expect(picker.value).toBe("lognormal_ewma")
  fireEvent.change(picker, { target: { value: "student_t_ewma" } })
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes("forecast_model=student_t_ewma"))).toBe(true))
  expect(localStorage.getItem("hyperoptions.forecastModel")).toBe("student_t_ewma")
  fireEvent.click(screen.getByRole("link", { name: "Watchlist" }))
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/watchlist?forecast_model=student_t_ewma")).toBe(true))
  expect((screen.getByRole("combobox", { name: "Stock forecast model" }) as HTMLSelectElement).value).toBe("student_t_ewma")
  mounted.unmount()
  render(<App />)
  expect((screen.getByRole("combobox", { name: "Stock forecast model" }) as HTMLSelectElement).value).toBe("student_t_ewma")
})
