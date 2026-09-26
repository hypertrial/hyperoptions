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
  const odds = await screen.findByRole("region", { name: "Odds estimates" })
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
  const odds = await screen.findByRole("region", { name: "Odds estimates" })
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
  await waitFor(() => expect(screen.getByRole("region", { name: "Odds estimates" }).textContent).toContain("63.8% ITM"))
  expect(reads).toBeGreaterThanOrEqual(2)
})

it("warns when a later watchlist read fails and clears the warning after retry", async () => {
  window.history.replaceState(null, "", "/watchlist")
  let reads = 0
  vi.stubGlobal("fetch", vi.fn(async () => {
    reads += 1
    if (reads === 2) throw new Error("Provider temporarily unavailable")
    return new Response(JSON.stringify({ items: [watched] }))
  }))

  render(<App />)
  expect(await screen.findByRole("region", { name: "Odds estimates" })).toBeTruthy()
  document.dispatchEvent(new Event("visibilitychange"))
  const warning = await screen.findByRole("alert")
  expect(warning.textContent).toContain("Showing the last loaded watchlist")
  expect(warning.textContent).toContain("Provider temporarily unavailable")
  expect(screen.getByRole("region", { name: "Odds estimates" })).toBeTruthy()

  fireEvent.click(screen.getByRole("button", { name: "Retry" }))
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull())
  expect(reads).toBe(3)
})

it("shares a pending retry with the next poll so responses cannot arrive out of order", async () => {
  window.history.replaceState(null, "", "/watchlist")
  let reads = 0
  let finishRetry: ((response: Response) => void) | undefined
  vi.stubGlobal("fetch", vi.fn(() => {
    reads += 1
    if (reads === 2) return Promise.reject(new Error("Provider temporarily unavailable"))
    if (reads === 3) return new Promise<Response>((resolve) => { finishRetry = resolve })
    return Promise.resolve(new Response(JSON.stringify({ items: [watched] })))
  }))

  render(<App />)
  expect(await screen.findByRole("region", { name: "Odds estimates" })).toBeTruthy()
  document.dispatchEvent(new Event("visibilitychange"))
  expect(await screen.findByRole("alert")).toBeTruthy()
  fireEvent.click(screen.getByRole("button", { name: "Retry" }))
  await waitFor(() => expect(finishRetry).toBeTypeOf("function"))
  document.dispatchEvent(new Event("visibilitychange"))
  expect(reads).toBe(3)

  await act(async () => { finishRetry!(new Response(JSON.stringify({ items: [watched] }))) })
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull())
  expect(reads).toBe(3)
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
  const odds = await screen.findByRole("region", { name: "Odds estimates" })
  expect(odds.textContent).toContain("Calibration failed")
  expect(odds.textContent).not.toContain("54.1%")
  expect(screen.queryByText(/Historical pre-expiry forecast/)).toBeNull()
})

it("uses a dated historical forecast when market bounds fail and keeps prior market odds as context", async () => {
  window.history.replaceState(null, "", "/watchlist")
  const item = {
    ...watched,
    market_odds: { ...watched.market_odds, status: "unavailable", itm_pct_tenths: null, otm_pct_tenths: null, reason: "Bounds too wide", session_date: "2026-09-18" },
    last_available_market_odds: watched.market_odds,
    predictive_odds: {
      status: "available", method: "lognormal_ewma", reason: null,
      itm_pct_tenths: 520, otm_pct_tenths: 480, atm_pct_tenths: 0,
      as_of_session: "2026-09-18", expiry_session: "2026-10-16",
      model_version: "lognormal-ewma60-v1", support: 60, data_hash: "test-hash",
    },
    hypothetical_risk: {
      status: "available", reason: null, assumed_spot_cents: 4900, assumed_bid_cents: 125,
      quote_source: "nasdaq", quote_session: "2026-09-18", expected_pnl_cents: 765,
      expected_return_pct_tenths: 16, loss_pct_tenths: 342, p05_pnl_cents: -9300,
    },
  }
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [item] }))))

  render(<App />)
  const odds = await screen.findByRole("region", { name: "Odds estimates" })
  expect(odds.querySelector(".watch-odds")?.textContent).toContain("Historical predictive")
  expect(odds.querySelector(".watch-odds")?.textContent).toContain("52.0% ITM")
  expect(odds.querySelector(".watch-odds")?.textContent).not.toContain("63.8%")
  expect(odds.textContent).toContain("completed stock closes through 2026-09-18")
  expect(odds.textContent).toContain("Bounds too wide")
  const prior = odds.querySelector(".watch-prior-odds") as HTMLDetailsElement
  expect(prior.open).toBe(false)
  expect(prior.textContent).toContain("session 2026-09-17")

  const risk = screen.getByRole("region", { name: "Hypothetical expiry risk" })
  expect(risk.textContent).toContain("Assumed stock entry")
  expect(risk.textContent).toContain("$49.00 per share")
  expect(risk.textContent).toContain("$1.25 per share")
  expect(risk.textContent).toContain("2026-09-18")
  expect(risk.textContent).toContain("$7.65")
  expect(risk.textContent).toContain("34.2%")
  expect(risk.textContent).toContain("-$93.00")
  expect(risk.textContent).toContain("No trade or position is recorded")
})

it("labels the underlying quote for a cash-secured put without implying a stock purchase", async () => {
  window.history.replaceState(null, "", "/watchlist")
  const item = {
    ...watched,
    side: "put",
    hypothetical_risk: {
      status: "available", assumed_spot_cents: 4900, assumed_bid_cents: 125,
      quote_source: "nasdaq", quote_session: "2026-09-18", expected_pnl_cents: 765,
      expected_return_pct_tenths: 16, loss_pct_tenths: 342, p05_pnl_cents: -9300,
    },
  }
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [item] }))))

  render(<App />)
  const risk = await screen.findByRole("region", { name: "Hypothetical expiry risk" })
  expect(risk.textContent).toContain("Underlying quote used")
  expect(risk.textContent).toContain("$49.00 per share")
  expect(risk.textContent).not.toContain("Assumed stock entry")
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
