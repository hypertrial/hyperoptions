import { expect, test } from "@playwright/test"

function watchNasdaq(page: import("@playwright/test").Page) {
  const nasdaqHits: string[] = []
  page.on("request", (request) => {
    if (request.url().includes("api.nasdaq.com")) nasdaqHits.push(request.url())
  })
  return nasdaqHits
}

async function chooseTicker(page: import("@playwright/test").Page, symbol: string) {
  const picker = page.getByRole("combobox", { name: "Ticker" })
  await picker.click()
  await picker.fill(symbol)
  const option = page.getByRole("option", { name: new RegExp(symbol) })
  await option.waitFor({ state: "visible" })
  await option.click()
}

test("loads the IREN chain through the Vite proxy and copies a row", async ({ page }) => {
  const nasdaqHits = watchNasdaq(page)

  await page.goto("/")
  await expect(page.getByRole("heading", { name: /2026-09-18/ })).toBeVisible()
  await expect(page.getByText("$48.00")).toBeVisible()
  await page.getByRole("button", { name: /Copy row IREN 2026-09-18 strike \$48.00/ }).click()
  await expect(page.getByText("Copied")).toBeVisible()
  const clipboard = await page.evaluate(() => navigator.clipboard.readText())
  expect(clipboard).toContain("IREN · 2026-09-18")
  expect(clipboard).toContain("1 contract · 100 sh")
  expect(clipboard).toContain("$48.00")
  expect(nasdaqHits).toEqual([])
})

test("proxies /api through Vite to FastAPI", async ({ request }) => {
  const response = await request.get("/api/health")
  expect(response.ok()).toBeTruthy()
  expect(await response.json()).toEqual({ ok: true })
  const tickers = await request.get("/api/tickers?q=IRE")
  expect(tickers.ok()).toBeTruthy()
  expect((await tickers.json()).results[0].symbol).toBe("IREN")
})

test("labels the browser-local fetch time beside the ET quote time", async ({ browser }) => {
  const context = await browser.newContext({ baseURL: "http://127.0.0.1:5173", timezoneId: "Europe/Zurich" })
  const page = await context.newPage()
  try {
    await page.goto("/")
    const quote = page.getByRole("region", { name: "IREN market summary" })
    await expect(quote).toContainText("Sep 11, 2026 10:00 AM ET")
    await expect(quote).toContainText("Sep 11, 2026, 4:00 PM GMT+2")
  } finally {
    await context.close()
  }
})

test("reveals overflowing tablet metrics to pointer and keyboard users", async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 900 })
  await page.goto("/")
  const scroll = page.locator(".table-scroll").first()
  await expect(scroll).toBeVisible()
  await expect(page.getByText("Scroll table sideways to view more metrics").first()).toBeVisible()
  await expect(scroll).toHaveAttribute("tabindex", "0")
  const actionBounds = await scroll.evaluate((node) => {
    const row = node.querySelector("tbody tr")!
    return {
      watchButtonRight: row.querySelector(".watch-cell button")!.getBoundingClientRect().right,
      copyCellLeft: row.querySelector(".copy-cell")!.getBoundingClientRect().left,
    }
  })
  expect(actionBounds.watchButtonRight).toBeLessThanOrEqual(actionBounds.copyCellLeft)
  await scroll.focus()
  await page.keyboard.press("ArrowRight")
  await expect.poll(() => scroll.evaluate((node) => node.scrollLeft)).toBeGreaterThan(0)

  await page.setViewportSize({ width: 2200, height: 900 })
  await expect(page.getByText("Scroll table sideways to view more metrics")).toHaveCount(0)
  await expect(scroll).not.toHaveAttribute("tabindex", "0")
})

test("surfaces a controlled provider error and keeps Nasdaq out of the browser", async ({ page }) => {
  const nasdaqHits = watchNasdaq(page)
  await page.goto("/")
  await expect(page.getByRole("heading", { name: /2026-09-18/ })).toBeVisible()
  await chooseTicker(page, "WULF")
  await expect(page.getByRole("alert")).toContainText("Couldn’t load WULF market data")
  await expect(page.getByRole("alert")).toContainText("Nasdaq unavailable")
  await expect(page.getByRole("button", { name: "Filters" })).toHaveCount(0)
  expect(nasdaqHits).toEqual([])
})

test("searches a ticker, toggles strategy and moneyness, and round-trips the URL", async ({ page }) => {
  const nasdaqHits = watchNasdaq(page)
  await page.goto("/")
  await expect(page.getByRole("heading", { name: /2026-09-18/ })).toBeVisible()
  await chooseTicker(page, "CIFR")
  await expect(page.getByRole("heading", { name: "Covered calls" })).toBeVisible()
  await page.getByRole("radio", { name: "Cash-secured puts" }).click()
  await expect(page.getByRole("heading", { name: "Cash-secured puts" })).toBeVisible()
  await page.getByRole("radio", { name: "ITM", exact: true }).click()
  await expect(page).toHaveURL(/t=CIFR/)
  await expect(page).toHaveURL(/side=put/)
  await expect(page).toHaveURL(/m=itm/)
  await expect(page).not.toHaveURL(/cols=/)
  await expect(page.getByRole("columnheader", { name: "Premium (net)" })).toBeVisible()
  await expect(page.getByRole("columnheader", { name: "% to assignment" })).toBeVisible()
  await page.reload()
  await expect(page.getByRole("heading", { name: "Cash-secured puts" })).toBeVisible()
  await expect(page.getByRole("radio", { name: "Cash-secured puts" })).toBeChecked()
  await expect(page.getByRole("radio", { name: "ITM", exact: true })).toBeChecked()
  expect(nasdaqHits).toEqual([])
})

test("persists theme without writing it to the URL", async ({ page }) => {
  const nasdaqHits = watchNasdaq(page)
  await page.goto("/")
  await expect(page.getByRole("heading", { name: /2026-09-18/ })).toBeVisible()
  await page.getByRole("button", { name: "Theme: system" }).click()
  await expect(page.locator("html")).toHaveClass(/dark/)
  await expect(page).not.toHaveURL(/theme=/)
  await page.reload()
  await expect(page.locator("html")).toHaveClass(/dark/)
  expect(nasdaqHits).toEqual([])
})

test("honors reduced-motion preferences in the settings drawer", async ({ page }) => {
  const nasdaqHits = watchNasdaq(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.emulateMedia({ reducedMotion: "reduce" })
  await page.goto("/")

  await page.getByRole("button", { name: "Settings", exact: true }).click()
  const drawer = page.locator(".drawer-popup")
  await expect(drawer).toBeVisible()
  const transitionSeconds = await drawer.evaluate((element) => Number.parseFloat(getComputedStyle(element).transitionDuration))
  expect(transitionSeconds).toBeLessThanOrEqual(0.00001)
  expect(nasdaqHits).toEqual([])
})

test("sorts strikes within an expiry group", async ({ page }) => {
  const nasdaqHits = watchNasdaq(page)
  await page.goto("/")
  await expect(page.getByRole("heading", { name: /2026-09-18/ })).toBeVisible()
  const firstRow = page.getByRole("row").nth(1)
  const before = await firstRow.getByRole("rowheader").textContent()
  await page.getByRole("button", { name: "Strike" }).first().click()
  await expect(firstRow.getByRole("rowheader")).not.toHaveText(before ?? "")
  expect(nasdaqHits).toEqual([])
})

test("maps non-optionable and missing symbols without contacting Nasdaq from the browser", async ({ page, request }) => {
  const nasdaqHits = watchNasdaq(page)
  const none = await request.get("/api/covered-calls/NONE")
  expect(none.status()).toBe(404)
  await page.goto("/?t=NOOPT")
  await expect(page.getByText("Options are not available for NOOPT")).toBeVisible()
  expect(nasdaqHits).toEqual([])
})

test("keeps unavailable odds reasons accessible without filling every compact row", async ({ page }) => {
  await page.route("**/api/covered-calls/IREN**", async (route) => {
    const response = await route.fetch()
    const chain = await response.json()
    for (const group of chain.expirations) {
      for (const contract of group.contracts) {
        contract.market_odds = { status: "unavailable", reason: "A coherent underlying bid and ask is unavailable" }
        contract.predictive_odds = { status: "unavailable", reason: "Insufficient completed history" }
      }
    }
    await route.fulfill({ response, json: chain })
  })
  await page.goto("/")
  const oddsCell = page.locator(".odds-cell").first()
  await expect(oddsCell).toContainText("Forecast unavailable")
  await expect(oddsCell.locator(".odds-reason p").first()).not.toBeVisible()
  await oddsCell.getByText("Why forecast unavailable?").click()
  await expect(oddsCell).toContainText("Insufficient completed history")
  await oddsCell.getByText("Why market odds unavailable?").click()
  await expect(oddsCell).toContainText("A coherent underlying bid and ask is unavailable")

  await page.setViewportSize({ width: 390, height: 844 })
  const row = page.locator(".mobile-option-row").first()
  await expect(row.locator(".mobile-row-summary")).toContainText("Forecast unavailable")
  await expect(row.locator(".mobile-row-summary")).not.toContainText("A coherent underlying bid and ask is unavailable")
  await row.getByRole("button", { name: /Show details for IREN/ }).click()
  await expect(row.locator(".mobile-row-details")).toContainText("A coherent underlying bid and ask is unavailable")
})

test("compares every model on a narrow screen without nesting the disclosure trigger", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.route("**/api/covered-calls/IREN**", async (route) => {
    const response = await route.fetch()
    const chain = await response.json()
    const contract = chain.expirations[0].contracts[0]
    contract.predictive_odds = { status: "available", method: "student_t_ewma", itm_pct_tenths: 611, otm_pct_tenths: 389, atm_pct_tenths: 0, evidence_key: "student_t_ewma:2-5" }
    contract.physical_models = [
      { status: "available", method: "lognormal_ewma", itm_pct_tenths: 600, otm_pct_tenths: 400, atm_pct_tenths: 0 },
      { status: "available", method: "empirical_scaled", itm_pct_tenths: 620, otm_pct_tenths: 380, atm_pct_tenths: 0 },
      { status: "available", method: "student_t_ewma", itm_pct_tenths: 611, otm_pct_tenths: 389, atm_pct_tenths: 0, evidence_key: "student_t_ewma:2-5" },
      { status: "pending", method: "gjr_garch_t", reason: "candidate_not_prepared" },
      { status: "unavailable", method: "intraday_shadow", reason: "stale_quote" },
    ]
    contract.market_models = [
      { status: "available", method: "regimelib", itm_pct_tenths: 580, otm_pct_tenths: 420 },
      { status: "unavailable", method: "constrained_call_curve", reason: "sparse_strikes" },
    ]
    chain.model_evidence = { "student_t_ewma:2-5": { prospective: { generated_at: "2026-09-27T12:00:00Z", model_version: "student-v1", input_version: "forecast-ledger-v1", tickers: 0, independent_date_blocks: 0, ticker_origin_horizon_units: 0, contract_forecasts_available: 0, contract_cells_attempted: 0, brier: { baseline: null, candidate: null }, log_loss: { baseline: null, candidate: null }, calibration_by_side: { call: [], put: [] }, latency_ms: {} }, retrospective: null } }
    await route.fulfill({ response, json: chain })
  })
  await page.goto("/")
  await page.getByRole("combobox", { name: "Stock forecast model" }).selectOption("student_t_ewma")
  const first = page.locator(".mobile-option-row").first()
  await expect(first.locator(".odds-physical")).toContainText("Student-t EWMA")
  expect(await first.evaluate((row) => row.querySelector(".mobile-row-summary")!.contains(row.querySelector(".mobile-model-compare button")))).toBe(false)
  await first.getByText("Compare models").click()
  const comparison = page.getByRole("dialog", { name: "Compare models" })
  await expect(comparison).toContainText("GJR-GARCH Student-t")
  await comparison.locator(".model-result").nth(2).locator("summary.model-result-heading").click()
  await expect(comparison).toContainText("N=0")
  await expect(comparison).toContainText("sparse strikes")
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
})

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1024, height: 768 },
  { width: 768, height: 900 },
  { width: 390, height: 844 },
  { width: 320, height: 800 },
]) {
  test(`uses the responsive workstation at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    const nasdaqHits = watchNasdaq(page)
    await page.setViewportSize(viewport)
    await page.goto("/")
    await expect(page.getByRole("heading", { name: "Covered calls" })).toBeVisible()
    await expect(page.getByRole("heading", { name: /2026-09-18/ })).toBeVisible()
    const sidebarOptionsFit = () => page.locator(".market-control-stack .segmented [role='radio']").evaluateAll((options) =>
      options.length === 4 && options.every((option) => option.clientHeight >= 44
        && option.scrollWidth <= option.clientWidth
        && option.scrollHeight <= option.clientHeight),
    )

    if (viewport.width >= 1024) {
      await expect(page.getByRole("complementary", { name: "Market analysis settings" })).toBeVisible()
      await expect(page.getByRole("button", { name: "Settings" })).toHaveCount(0)
      await page.getByRole("radio", { name: "Cash-secured puts" }).click()
      await expect.poll(sidebarOptionsFit).toBe(true)
    } else {
      const trigger = page.getByRole("button", { name: "Settings", exact: true })
      await expect(trigger).toBeVisible()
      await expect(page.locator(".mobile-market-quote .market-session-status strong")).toHaveText(/^(Open at fetch|Closed at fetch|Unavailable)$/)
      await trigger.click()
      await expect(page.getByRole("heading", { name: "Market setup" })).toBeVisible()
      const putStrategy = page.getByRole("radio", { name: "Cash-secured puts" })
      await putStrategy.click()
      await expect(putStrategy).toBeChecked()
      await expect(page).toHaveURL(/side=put/)
      await expect.poll(sidebarOptionsFit).toBe(true)
      await page.keyboard.press("Escape")
      await expect(trigger).toBeFocused()
    }

    await expect(page.getByRole("heading", { name: "Cash-secured puts" })).toBeVisible()
    await expect.poll(async () => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    if (viewport.width < 640) {
      await expect(page.getByRole("table")).toHaveCount(0)
      const firstDisclosure = page.getByRole("button", { name: /Show details for/ }).first()
      await expect(firstDisclosure).toBeVisible()
      if (viewport.width === 390) {
        const mobileSort = page.getByLabel("Sort by")
        await mobileSort.selectOption("put_bid_cents")
        await expect(mobileSort).toHaveValue("put_bid_cents")
        await firstDisclosure.click()
        const copy = page.getByRole("button", { name: /Copy row/ }).first()
        await copy.click()
        await expect(copy).toContainText("Copied")
        const clipboard = await page.evaluate(() => navigator.clipboard.readText())
        expect(clipboard).toContain("IREN · 2026-09-18")
        expect(clipboard).toContain("Breakeven")
        expect(clipboard).not.toContain("Cushion (BE)")
      }
      if (viewport.width === 320) {
        await expect(page.locator(".expiry-heading").first().getByText(/contracts$/)).toBeVisible()
        await expect(page.getByRole("button", { name: /Columns/ })).toHaveCount(0)
        await expect(page.getByRole("button", { name: /Show details for/ }).first()).toBeVisible()
      }
    } else {
      await expect(page.getByRole("table")).toBeVisible()
    }
    expect(nasdaqHits).toEqual([])
  })
}
