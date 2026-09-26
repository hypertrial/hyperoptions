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
  forecast: {
    status: "available",
    itm_probability: 0.62,
    reason: null,
    as_of: "2026-09-17",
    model_id: "model-1",
    strategy_id: "rule-1",
    strategy_name: "Trend rule",
    signal_state: "long",
    fit_peers: 75,
    audit_peers: 20,
    audit_blocks: 35,
    crps_skill_lower_90: 0.02,
    brier_delta: -0.01,
    source: "Yahoo Finance daily Close (auto_adjust=False)",
    survivorship_note: "Current-listings cohort has survivorship bias.",
  },
  outcome: {
    status: "provisional",
    classification: "itm",
    reason: null,
    source: "Yahoo Finance daily Close",
    session_date: "2026-09-18",
    retrieved_at: "2026-09-19T14:00:00Z",
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

test("opens a watchlist deep link with separate forecast and result and tracks refresh progress", async ({ page }) => {
  let completeJob = false
  await page.route("**/api/watchlist", async (route) => {
    await route.fulfill({ json: { items: [item] } })
  })
  await page.route("**/api/watchlist/refresh", async (route) => {
    await route.fulfill({ json: { job: { id: "job-1", kind: "watch", state: "queued", progress: 0, message: "queued" } } })
  })
  await page.route("**/api/jobs/job-1", async (route) => {
    await route.fulfill({ json: {
      id: "job-1", kind: "watch", state: completeJob ? "succeeded" : "running",
      progress: completeJob ? 1 : 0.4, message: completeJob ? "done" : "Preparing forecasts",
    } })
  })

  await page.goto("/watchlist")
  await expect(page.getByRole("heading", { name: "Watchlist" })).toBeVisible()
  await expect(page.getByRole("region", { name: "Pre-expiry forecast" })).toContainText("62.0% ITM probability")
  await expect(page.getByRole("region", { name: "Expiry result" })).toContainText("Provisional ITM")
  await expect(page.getByRole("region", { name: "Expiry result" })).toContainText("Yahoo Finance daily Close")
  await expect(page.getByText(/not an OCC exercise or assignment decision/)).toBeVisible()

  await page.getByRole("button", { name: "Refresh watchlist" }).click()
  await expect(page.getByRole("progressbar", { name: "Watchlist refresh progress" })).toHaveAttribute("aria-valuenow", "40")
  completeJob = true
  await expect(page.getByText("Watchlist updated.")).toBeVisible()
})
