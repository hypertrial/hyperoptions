// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import App from "../App"
import type { WatchItem } from "./types"

const watched: WatchItem = {
  id: "watch-1",
  ticker: "IREN",
  root: "IREN",
  side: "call",
  expiration: "2026-09-18",
  strike_exact: "50.000",
  terms_note: "Assuming standard 100-share terms.",
  created_at: "2026-09-11T14:00:00Z",
  forecast: {
    status: "available",
    itm_probability: 0.638,
    reason: null,
    as_of: "2026-09-17",
    model_id: "model-1",
    strategy_id: "strategy-1",
    strategy_name: "Trend and volume",
    signal_state: "long",
    cohort_size: 88,
    fit_peers: 71,
    audit_peers: 22,
    fit_samples: 9150,
    audit_samples: 2100,
    audit_blocks: 36,
    crps_skill_lower_90: 0.018,
    brier_delta: -0.012,
    source: "Yahoo Finance daily Close (auto_adjust=False)",
    survivorship_note: "Current-listings cohort has survivorship bias.",
  },
  outcome: {
    status: "provisional",
    classification: "atm",
    reason: null,
    source: "Yahoo Finance daily Close",
    session_date: "2026-09-18",
    retrieved_at: "2026-09-19T14:00:00Z",
    close_exact: "50.000",
    terms_note: "Assuming standard 100-share terms.",
  },
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.sessionStorage.clear()
})

it("opens the watchlist deep link with a dated forecast and separate provisional expiry result", async () => {
  window.history.replaceState(null, "", "/watchlist")
  const fetchMock = vi.fn(async () => new Response(JSON.stringify({ items: [watched] }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))
  vi.stubGlobal("fetch", fetchMock)

  render(<App />)

  expect(await screen.findByRole("heading", { name: "Watchlist" })).toBeTruthy()
  expect(screen.getByRole("link", { name: "Watchlist" }).getAttribute("aria-current")).toBe("page")
  expect(screen.getByText(/Watches are for tracking contracts/)).toBeTruthy()
  await screen.findByRole("region", { name: "Pre-expiry forecast" })
  expect(screen.getByRole("region", { name: "Pre-expiry forecast" }).textContent).toContain("63.8% ITM probability")
  expect(screen.getByRole("region", { name: "Pre-expiry forecast" }).textContent).toContain("2026-09-17")
  expect(screen.getByRole("region", { name: "Pre-expiry forecast" }).textContent).toContain("Holdout model-skill uncertainty")
  expect(screen.getByRole("region", { name: "Pre-expiry forecast" }).textContent).toContain("88 peers")
  expect(screen.getByRole("region", { name: "Pre-expiry forecast" }).textContent).toContain("9,150 / 2,100")
  expect(screen.getByRole("region", { name: "Expiry result" }).textContent).toContain("Provisional ATM")
  expect(screen.getByRole("region", { name: "Expiry result" }).textContent).toContain("$50.000")
  expect(screen.getByText(/not an OCC exercise or assignment decision/)).toBeTruthy()
  expect(fetchMock).toHaveBeenCalledWith("/api/watchlist", expect.anything())
})

it("shows a close recheck warning while preserving a sourced provisional result", async () => {
  window.history.replaceState(null, "", "/watchlist")
  const changedTerms: WatchItem = {
    ...watched,
    outcome: {
      ...watched.outcome,
      reason: "A later stock split makes this close-based result uncertain.",
    },
  }
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [changedTerms], active_job: null }))))
  render(<App />)
  expect(await screen.findByText(/Provisional ATM/)).toBeTruthy()
  expect(screen.getByRole("alert").textContent).toContain("A later stock split makes this close-based result uncertain")
})

it("shows unavailable evidence and tracks a background refresh without inventing a probability", async () => {
  window.history.replaceState(null, "", "/watchlist")
  const unavailable: WatchItem = {
    ...watched,
    forecast: { ...watched.forecast!, status: "unavailable", itm_probability: null, reason: "ticker_history_short" },
    outcome: { ...watched.outcome, status: "pending", classification: null, reason: "Waiting for completed session", close_exact: null },
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === "/api/watchlist/refresh" && init?.method === "POST") return new Response(JSON.stringify({ job: { id: "job-1", kind: "watch", state: "queued", progress: 0, message: "queued" } }))
    if (url === "/api/jobs/job-1") return new Response(JSON.stringify({ id: "job-1", kind: "watch", state: "running", progress: 0.4, message: "Preparing forecast" }))
    return new Response(JSON.stringify({ items: [unavailable] }))
  })
  vi.stubGlobal("fetch", fetchMock)

  render(<App />)
  expect(await screen.findByText(/Probability unavailable/)).toBeTruthy()
  expect(screen.queryByText(/63.8%/)).toBeNull()
  expect(screen.getByText(/too little completed price history/)).toBeTruthy()
  expect(screen.getByText(/Expiry result pending/)).toBeTruthy()

  fireEvent.click(screen.getByRole("button", { name: "Refresh watchlist" }))
  await waitFor(() => expect(screen.getByRole("progressbar", { name: "Watchlist refresh progress" }).getAttribute("aria-valuenow")).toBe("40"))
  expect(screen.getByText(/Preparing forecast/)).toBeTruthy()
  expect(fetchMock).toHaveBeenCalledWith("/api/watchlist/refresh", expect.objectContaining({ method: "POST", body: "{}" }))
})

it("explains failed audit gates and future expiry without publishing a probability", async () => {
  window.history.replaceState(null, "", "/watchlist")
  const failed: WatchItem = {
    ...watched,
    expiration: "2026-10-16",
    forecast: {
      ...watched.forecast!, status: "unavailable", itm_probability: null,
      reason: "crps_audit_failed", as_of: "2026-09-25", strategy_name: "ROC",
      cohort_size: 100, fit_peers: 75, audit_peers: 25, audit_blocks: 50,
      fit_samples: 16189, audit_samples: 584,
      crps_skill_lower_90: -0.0033024768, brier_delta: 0.0007887774,
    },
    outcome: {
      ...watched.outcome, status: "pending", classification: null,
      reason: "Expiry trading session has not completed", session_date: "2026-10-16",
      close_exact: null,
    },
  }
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [failed] }))))

  render(<App />)
  const forecast = await screen.findByRole("region", { name: "Pre-expiry forecast" })
  expect(forecast.textContent).toContain("Probability unavailable")
  expect(forecast.textContent).toContain("holdout audit")
  expect(forecast.textContent).toContain("2026-09-25")
  expect(forecast.textContent).toContain("ROC")
  expect(forecast.textContent).toContain("75")
  expect(forecast.textContent).toContain("25 / 50")
  expect(forecast.textContent).toContain("16,189 / 584")
  expect(forecast.textContent).toContain("-0.0033")
  expect(forecast.textContent).toContain("+0.000789")
  expect(forecast.textContent).toContain("needs > 0")
  expect(forecast.textContent).toContain("needs ≤ 0")
  expect(forecast.textContent).not.toContain("crps audit failed")
  expect(forecast.textContent).not.toContain("63.8%")
  const expiry = screen.getByRole("region", { name: "Expiry result" })
  expect(expiry.textContent).toContain("Not expired yet")
  expect(expiry.textContent).toContain("2026-10-16")
  expect(expiry.textContent).not.toContain("Awaiting first expiry check")
})

it("discovers an automatic refresh and shows its result without a page reload", async () => {
  window.history.replaceState(null, "", "/watchlist")
  let listReads = 0
  let jobReads = 0
  const pending: WatchItem = {
    ...watched,
    forecast: { ...watched.forecast!, status: "unavailable", itm_probability: null, reason: "model_not_ready" },
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === "/api/watchlist") {
      listReads += 1
      return new Response(JSON.stringify({
        items: [listReads < 3 ? pending : watched],
        active_job: listReads === 2
          ? { id: "automatic-1", kind: "watch_refresh", state: "running", progress: 0.2, message: "Preparing peers" }
          : null,
      }))
    }
    if (String(input) === "/api/jobs/automatic-1") {
      jobReads += 1
      return new Response(JSON.stringify({
        id: "automatic-1", kind: "watch_refresh",
        state: jobReads === 1 ? "running" : "succeeded",
        progress: jobReads === 1 ? 0.4 : 1,
        message: jobReads === 1 ? "Selecting peers" : "done",
      }))
    }
    throw new Error(`unexpected request ${String(input)}`)
  })
  vi.stubGlobal("fetch", fetchMock)

  render(<App />)
  expect(await screen.findByText(/Probability unavailable/)).toBeTruthy()
  document.dispatchEvent(new Event("visibilitychange"))
  await waitFor(() => expect(screen.getByRole("progressbar", { name: "Watchlist refresh progress" }).getAttribute("aria-valuenow")).toBe("40"))
  await waitFor(() => {
    expect(screen.getByRole("region", { name: "Pre-expiry forecast" }).textContent).toContain("63.8% ITM probability")
  }, { timeout: 4000 })
  expect(jobReads).toBeGreaterThanOrEqual(2)
  expect(listReads).toBeGreaterThanOrEqual(3)
})

it("keeps an older passing forecast visible after the latest snapshot becomes unavailable", async () => {
  window.history.replaceState(null, "", "/watchlist")
  const withHistory: WatchItem = {
    ...watched,
    forecast: { ...watched.forecast!, status: "unavailable", itm_probability: null, reason: "stale_model" },
    last_available_forecast: { ...watched.forecast!, as_of: "2026-09-16", itm_probability: 0.541, historical: true },
  }
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [withHistory] }))))

  render(<App />)
  const forecast = await screen.findByRole("region", { name: "Pre-expiry forecast" })
  expect(forecast.textContent).toContain("Probability unavailable")
  expect(forecast.textContent).toContain("validated model is too old")
  expect(forecast.textContent).toContain("Historical pre-expiry forecast")
  expect(forecast.textContent).toContain("54.1% ITM probability")
  expect(forecast.textContent).toContain("2026-09-16")
  expect(screen.getByRole("region", { name: "Expiry result" }).textContent).toContain("Provisional ATM")
})

it("labels a stored forecast historical while the expiry close is still pending", async () => {
  window.history.replaceState(null, "", "/watchlist")
  const closePending: WatchItem = {
    ...watched,
    outcome: {
      ...watched.outcome,
      status: "pending",
      classification: null,
      reason: "Yahoo has not supplied the expiry-session Close",
      close_exact: null,
    },
  }
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [closePending] }))))

  render(<App />)
  const forecast = await screen.findByRole("region", { name: "Pre-expiry forecast" })
  expect(forecast.textContent).toContain("Historical pre-expiry forecast")
  expect(forecast.textContent).toContain("historical, not a current probability")
  expect(screen.getByRole("region", { name: "Expiry result" }).textContent).toContain("Expiry result pending")
})

it("removes a watched contract through the watchlist API", async () => {
  window.history.replaceState(null, "", "/watchlist")
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/api/watchlist/watch-1" && init?.method === "DELETE") return new Response(null, { status: 204 })
    return new Response(JSON.stringify({ items: [watched] }))
  })
  vi.stubGlobal("fetch", fetchMock)

  render(<App />)
  fireEvent.click(await screen.findByRole("button", { name: /Remove IREN call 2026-09-18/ }))
  expect(await screen.findByRole("heading", { name: "No watched contracts yet" })).toBeTruthy()
  expect(fetchMock).toHaveBeenCalledWith("/api/watchlist/watch-1", expect.objectContaining({ method: "DELETE" }))
})
