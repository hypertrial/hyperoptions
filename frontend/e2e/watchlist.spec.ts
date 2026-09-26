import { expect, test, type Page } from "@playwright/test"

const item = {
  id: "watch-1",
  ticker: "IREN",
  root: "IREN",
  side: "call",
  expiration: "2026-09-18",
  strike_exact: "48.000",
  terms_note: "Assuming standard 100-share terms.",
  created_at: "2026-09-11T14:00:00Z",
  market_odds: {
    status: "available",
    itm_pct_tenths: 620,
    otm_pct_tenths: 380,
    reason: null,
    source: "nasdaq",
    fetched_at: "2026-09-11T14:00:00Z",
    session_date: "2026-09-11",
    model_version: "regimelib-0.1.0-market-odds-v1",
  },
  outcome: {
    status: "pending",
    classification: null,
    reason: "Expiry trading session has not completed",
    session_date: "2026-09-18",
  },
}

const expiredItem = {
  ...item,
  id: "watch-expired",
  expiration: "2026-09-11",
  market_odds: {
    status: "unavailable",
    itm_pct_tenths: null,
    otm_pct_tenths: null,
    reason: "Expiry session completed; see outcome",
    source: null,
    fetched_at: null,
    session_date: null,
    model_version: null,
  },
  outcome: {
    status: "provisional",
    classification: "itm",
    reason: null,
    source: "Yahoo Finance daily Close",
    session_date: "2026-09-11",
    retrieved_at: "2026-09-12T14:00:00Z",
    close_exact: "50.000",
    terms_note: "Assuming standard 100-share terms.",
  },
}

async function watchableChain(page: Page) {
  await page.route("**/api/covered-calls/IREN**", async (route) => {
    const response = await route.fetch()
    const chain = await response.json()
    for (const group of chain.expirations) {
      for (const contract of group.contracts) {
        contract.watch_key = `opaque:${group.expiration}:${contract.strike_cents}`
        contract.watchability_reason = null
        if (group.expiration === "2026-09-18" && contract.strike_cents === 4800) {
          contract.market_odds = item.market_odds
        }
      }
    }
    await route.fulfill({ response, json: chain })
  })
}

test("watches a desktop chain contract and restores its URL after visiting the watchlist", async ({ page }) => {
  await watchableChain(page)
  const posted: string[] = []
  await page.route("**/api/watchlist", async (route) => {
    if (route.request().method() === "POST") {
      posted.push((route.request().postDataJSON() as { watch_key: string }).watch_key)
      await route.fulfill({ json: { item, created: true, job: null } })
    } else {
      await route.fulfill({ json: { items: [item] } })
    }
  })

  await page.goto("/?t=IREN&side=call&m=itm&cols=strike_cents")
  await expect(page.getByRole("columnheader", { name: "Watch" })).toBeVisible()
  await expect(page.getByRole("columnheader", { name: "ITM / OTM odds" })).toBeVisible()
  await expect(page.getByText("62.0%", { exact: true }).first()).toBeVisible()
  await expect(page.getByText("38.0%", { exact: true }).first()).toBeVisible()
  const watch = page.getByRole("button", { name: /Watch IREN 2026-09-18 .* strike/ }).first()
  await watch.click()
  await expect(page.getByRole("button", { name: /Watching IREN 2026-09-18 .* strike/ }).first()).toBeDisabled()
  expect(posted).toHaveLength(1)
  expect(posted[0]).toMatch(/^opaque:2026-09-18:/)

  await page.getByRole("link", { name: "Watchlist", exact: true }).click()
  await expect(page.getByRole("heading", { name: "Watchlist" })).toBeVisible()
  await page.getByRole("link", { name: "Option chain", exact: true }).click()
  await expect(page).toHaveURL(/t=IREN&side=call&m=itm&cols=strike_cents/)
  await expect(page.getByRole("columnheader", { name: "Strike" })).toBeVisible()
})

test("watches a contract from a mobile disclosure and reports deduplication", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await watchableChain(page)
  await page.route("**/api/watchlist", async (route) => {
    await route.fulfill({ json: { item, created: false, job: null } })
  })

  await page.goto("/")
  const disclosure = page.getByRole("button", { name: /Show details for IREN/ }).first()
  await disclosure.click()
  const watch = page.getByRole("button", { name: /Watch IREN 2026-09-18 strike/ }).first()
  await expect(watch).toBeVisible()
  await watch.click()
  await expect(page.getByRole("button", { name: /Already watching IREN/ }).first()).toBeDisabled()
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test("shows dated market odds and close-based outcomes, then tracks the result check", async ({ page }) => {
  let completeJob = false
  await page.route("**/api/watchlist", async (route) => {
    await route.fulfill({ json: { items: [item, expiredItem] } })
  })
  await page.route("**/api/watchlist/refresh", async (route) => {
    await route.fulfill({ json: { job: { id: "job-1", kind: "watch_refresh", state: "queued", progress: 0, message: "queued" } } })
  })
  await page.route("**/api/jobs/job-1", async (route) => {
    await route.fulfill({ json: {
      id: "job-1", kind: "watch_refresh", state: completeJob ? "succeeded" : "running",
      progress: completeJob ? 1 : 0.4, message: completeJob ? "done" : "Checking expiry close",
    } })
  })

  await page.goto("/watchlist")
  await expect(page.getByRole("heading", { name: "Watchlist" })).toBeVisible()
  const active = page.getByRole("article").filter({
    has: page.getByRole("heading", { name: "Call · $48.000 · 2026-09-18" }),
  })
  await expect(active.getByRole("region", { name: "Odds estimates" })).toContainText("62.0% ITM")
  await expect(active.getByRole("region", { name: "Odds estimates" })).toContainText("38.0% OTM")
  await expect(active.getByRole("region", { name: "Odds estimates" })).toContainText("Nasdaq")
  await expect(active.getByRole("region", { name: "Odds estimates" })).toContainText("session 2026-09-11")
  await expect(active.getByRole("region", { name: "Expiry result" })).toContainText("Not expired yet")
  const expired = page.getByRole("article").filter({
    has: page.getByRole("heading", { name: "Call · $48.000 · 2026-09-11" }),
  })
  await expect(expired.getByRole("region", { name: "Odds estimates" })).toContainText("Expiry session completed")
  await expect(expired.getByRole("region", { name: "Expiry result" })).toContainText("Provisional ITM")
  await expect(expired.getByRole("region", { name: "Expiry result" })).toContainText("Yahoo Finance daily Close")
  await expect(page.getByText(/not an OCC exercise or assignment decision/)).toBeVisible()

  await page.getByRole("button", { name: "Check expiry results" }).click()
  await expect(page.getByRole("progressbar", { name: "Watchlist refresh progress" })).toHaveAttribute("aria-valuenow", "40")
  completeJob = true
  await expect(page.getByText("Expiry results checked.")).toBeVisible()
})

test("names unavailable odds before the reason and hides the model identifier", async ({ page }) => {
  await page.route("**/api/watchlist", async (route) => {
    await route.fulfill({ json: { items: [{ ...item, market_odds: {
      ...item.market_odds,
      status: "unavailable",
      itm_pct_tenths: null,
      otm_pct_tenths: null,
      reason: "A coherent underlying bid and ask is unavailable",
    } }] } })
  })
  await page.goto("/watchlist")
  const odds = page.getByRole("region", { name: "Odds estimates" })
  await expect(odds).toContainText("Odds unavailable")
  await expect(odds).toContainText("A coherent underlying bid and ask is unavailable")
  await expect(odds).not.toContainText("regimelib-0.1.0-market-odds-v1")
})

test("shows a separate forecast, prior market context, and dated hypothetical risk on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 })
  await page.route("**/api/watchlist", async (route) => {
    await route.fulfill({ json: { items: [{
      ...item,
      market_odds: { ...item.market_odds, status: "unavailable", itm_pct_tenths: null, otm_pct_tenths: null, reason: "Quote bounds too wide" },
      last_available_market_odds: item.market_odds,
      predictive_odds: {
        status: "available", method: "lognormal_ewma", itm_pct_tenths: 520,
        otm_pct_tenths: 480, atm_pct_tenths: 0, as_of_session: "2026-09-11",
        expiry_session: "2026-09-18", support: 60,
      },
      hypothetical_risk: {
        status: "available", assumed_spot_cents: 4990, assumed_bid_cents: 125,
        quote_source: "nasdaq", quote_session: "2026-09-11",
        expected_pnl_cents: 765, expected_return_pct_tenths: 16,
        loss_pct_tenths: 342, p05_pnl_cents: -9300,
      },
    }] } })
  })
  await page.goto("/watchlist")
  const odds = page.getByRole("region", { name: "Odds estimates" })
  await expect(odds).toContainText("Historical predictive")
  await expect(odds).toContainText("52.0% ITM")
  await expect(odds.getByText(/Previous market-implied estimate/)).toBeVisible()
  const risk = page.getByRole("region", { name: "Hypothetical expiry risk" })
  await expect(risk).toContainText("$49.90 per share")
  await expect(risk).toContainText("$7.65")
  await expect(risk).toContainText("2026-09-11")
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test("retired research deep links open the watchlist without research requests", async ({ page }) => {
  const researchRequests: string[] = []
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/research/")) {
      researchRequests.push(request.url())
    }
  })
  await page.goto("/research/strategies/old-rule?ticker=IREN&run=old")
  await expect(page).toHaveURL(/\/watchlist$/)
  await expect(page.getByRole("heading", { name: "Watchlist" })).toBeVisible()
  expect(researchRequests).toEqual([])
})

test("unknown paths show a usable recovery page", async ({ page }) => {
  await page.goto("/missing-page")
  const main = page.getByRole("main")
  await expect(main.getByRole("heading", { name: "Page not found" })).toBeVisible()
  await main.getByRole("link", { name: "go to the watchlist" }).click()
  await expect(page.getByRole("heading", { name: "Watchlist" })).toBeVisible()
})

test("keeps watched contracts visible after a later poll fails and clears the warning on retry", async ({ page }) => {
  let reads = 0
  await page.route("**/api/watchlist", async (route) => {
    if (route.request().method() !== "GET") return route.continue()
    reads += 1
    if (reads === 2) return route.fulfill({ status: 503, json: { detail: "Provider temporarily unavailable" } })
    return route.fulfill({ json: { items: [item] } })
  })

  await page.goto("/watchlist")
  await expect(page.getByRole("article")).toBeVisible()
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")))
  const warning = page.getByRole("alert")
  await expect(warning).toContainText("Showing the last loaded watchlist")
  await expect(warning).toContainText("Provider temporarily unavailable")
  await expect(page.getByRole("article")).toBeVisible()
  await warning.getByRole("button", { name: "Retry" }).click()
  await expect(warning).toHaveCount(0)
  expect(reads).toBe(3)
})
