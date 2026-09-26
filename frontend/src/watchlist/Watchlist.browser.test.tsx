// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import App from "../App"

const watched = {
  id: "watch-1",
  ticker: "IREN",
  root: "IREN",
  side: "call",
  expiration: "2026-10-16",
  strike_exact: "50.000",
  terms_note: "Assuming standard 100-share terms.",
  created_at: "2026-09-11T14:00:00Z",
  market_odds: {
    status: "available",
    itm_pct_tenths: 638,
    otm_pct_tenths: 362,
    reason: null,
    source: "nasdaq",
    fetched_at: "2026-09-17T14:00:00Z",
    session_date: "2026-09-17",
    model_version: "regimelib-0.1.0",
  },
  outcome: {
    status: "pending",
    classification: null,
    reason: "Expiry trading session has not completed",
    source: null,
    session_date: "2026-10-16",
    retrieved_at: null,
    close_exact: null,
    terms_note: "Assuming standard 100-share terms.",
  },
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.sessionStorage.clear()
})

it("shows dated market-implied ITM and OTM odds beside the expiry result", async () => {
  window.history.replaceState(null, "", "/watchlist")
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [watched] }))))

  render(<App />)
  const odds = await screen.findByRole("region", { name: "Market-implied odds" })
  expect(odds.textContent).toContain("63.8% ITM")
  expect(odds.textContent).toContain("36.2% OTM")
  expect(odds.textContent).toContain("Risk-neutral")
  expect(odds.textContent).toContain("Nasdaq")
  expect(odds.textContent).toContain("Sep 17, 2026")
  expect(odds.textContent).not.toContain("regimelib-0.1.0")
  expect(screen.getByRole("region", { name: "Expiry result" }).textContent).toContain("Not expired yet")
})

it("shows a reason without inventing odds and keeps the manual expiry check", async () => {
  window.history.replaceState(null, "", "/watchlist")
  const unavailable = {
    ...watched,
    market_odds: {
      ...watched.market_odds,
      status: "unavailable",
      itm_pct_tenths: null,
      otm_pct_tenths: null,
      reason: "Too few reliable option quotes to fit market odds.",
      fetched_at: null,
    },
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/api/watchlist/refresh" && init?.method === "POST") {
      return new Response(JSON.stringify({ job: null }))
    }
    return new Response(JSON.stringify({ items: [unavailable] }))
  })
  vi.stubGlobal("fetch", fetchMock)

  render(<App />)
  const odds = await screen.findByRole("region", { name: "Market-implied odds" })
  expect(odds.textContent).toContain("Odds unavailable")
  expect(odds.textContent).toContain("Too few reliable option quotes")
  expect(odds.textContent).not.toContain("63.8%")
  fireEvent.click(screen.getByRole("button", { name: "Check expiry results" }))
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/watchlist/refresh", expect.objectContaining({ method: "POST" }),
  ))
})

it("updates pending odds from the visible watchlist poll", async () => {
  window.history.replaceState(null, "", "/watchlist")
  let reads = 0
  const pending = {
    ...watched,
    market_odds: { ...watched.market_odds, status: "pending", itm_pct_tenths: null, otm_pct_tenths: null },
  }
  vi.stubGlobal("fetch", vi.fn(async () => {
    reads += 1
    return new Response(JSON.stringify({ items: [reads === 1 ? pending : watched] }))
  }))

  render(<App />)
  expect(await screen.findByText(/Calculating odds/)).toBeTruthy()
  document.dispatchEvent(new Event("visibilitychange"))
  await waitFor(() => expect(screen.getByRole("region", { name: "Market-implied odds" }).textContent).toContain("63.8% ITM"))
  expect(reads).toBeGreaterThanOrEqual(2)
})

it("does not revive an old real-world forecast when current market odds are unavailable", async () => {
  window.history.replaceState(null, "", "/watchlist")
  const item = {
    ...watched,
    market_odds: { ...watched.market_odds, status: "unavailable", itm_pct_tenths: null, otm_pct_tenths: null, reason: "Calibration failed." },
    forecast: { status: "available", itm_probability: 0.541 },
    last_available_forecast: { status: "available", itm_probability: 0.541 },
  }
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [item] }))))

  render(<App />)
  const odds = await screen.findByRole("region", { name: "Market-implied odds" })
  expect(odds.textContent).toContain("Calibration failed")
  expect(odds.textContent).not.toContain("54.1%")
  expect(screen.queryByText(/Historical pre-expiry forecast/)).toBeNull()
})

it("preserves a sourced provisional expiry result and removal action", async () => {
  window.history.replaceState(null, "", "/watchlist")
  const item = {
    ...watched,
    outcome: {
      ...watched.outcome,
      status: "provisional",
      classification: "atm",
      reason: "A later stock split makes this result uncertain.",
      source: "Yahoo Finance daily Close",
      session_date: "2026-10-16",
      retrieved_at: "2026-10-17T14:00:00Z",
      close_exact: "50.000",
    },
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/api/watchlist/watch-1" && init?.method === "DELETE") return new Response(null, { status: 204 })
    return new Response(JSON.stringify({ items: [item] }))
  })
  vi.stubGlobal("fetch", fetchMock)

  render(<App />)
  expect(await screen.findByText(/Provisional ATM/)).toBeTruthy()
  expect(screen.getByRole("alert").textContent).toContain("stock split")
  fireEvent.click(screen.getByRole("button", { name: /Remove IREN call 2026-10-16/ }))
  expect(await screen.findByRole("heading", { name: "No watched contracts yet" })).toBeTruthy()
})

it("ignores an older watchlist read that finishes after a watch is removed", async () => {
  window.history.replaceState(null, "", "/watchlist")
  let finishOldRead: ((response: Response) => void) | undefined
  let reads = 0
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/api/watchlist/watch-1" && init?.method === "DELETE") {
      return Promise.resolve(new Response(null, { status: 204 }))
    }
    reads += 1
    if (reads === 2) return new Promise<Response>((resolve) => { finishOldRead = resolve })
    const items = reads === 1 ? [watched] : [{ ...watched, id: "watch-2", ticker: "CIFR", root: "CIFR" }]
    return Promise.resolve(new Response(JSON.stringify({ items })))
  })
  vi.stubGlobal("fetch", fetchMock)

  render(<App />)
  expect(await screen.findByRole("button", { name: /Remove IREN call 2026-10-16/ })).toBeTruthy()
  document.dispatchEvent(new Event("visibilitychange"))
  await waitFor(() => expect(finishOldRead).toBeTypeOf("function"))
  fireEvent.click(screen.getByRole("button", { name: /Remove IREN call 2026-10-16/ }))
  expect(await screen.findByRole("heading", { name: "No watched contracts yet" })).toBeTruthy()

  await act(async () => { finishOldRead!(new Response(JSON.stringify({ items: [watched] }))) })
  expect(screen.queryByRole("button", { name: /Remove IREN call 2026-10-16/ })).toBeNull()
  expect(screen.getByRole("heading", { name: "No watched contracts yet" })).toBeTruthy()

  document.dispatchEvent(new Event("visibilitychange"))
  await waitFor(() => expect(reads).toBe(3))
  expect(await screen.findByRole("button", { name: /Remove CIFR call 2026-10-16/ })).toBeTruthy()
})
