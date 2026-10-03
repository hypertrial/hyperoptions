import { expect, test, type Page } from "@playwright/test"
import { sampleContract, sampleIvDetails, samplePage, samplePutContract, samplePutPage } from "../src/testFixtures"

function diagnosticPage(unavailable = false, count = 1) {
  const reason = { code: "option_quote" as const, message: "Option bid and ask are locked" }
  return samplePage({
    fetched_at: "2026-09-11T20:00:01Z",
    chain_fetched_at: "2026-09-11T20:00:01Z",
    quote_timestamp: "Sep 11, 2026 4:00 PM ET",
    current_cents: 9990,
    stock_bid_cents: 9990,
    stock_ask_cents: 10_010,
    expirations: [{ expiration: "2026-09-18", dte: 7, contracts: Array.from({ length: count }, (_, i) => sampleContract({
      strike_cents: 8000 + i,
      call_bid_cents: 2010,
      call_ask_cents: unavailable ? 2010 : 2030,
      iv_pct_tenths: unavailable ? null : 877,
      iv_details: sampleIvDetails({
        strike_exact: (80 + i / 100).toFixed(2),
        ...(unavailable ? {
          status: "unavailable", reason, bid_pct_tenths: null, ask_pct_tenths: null,
          bid_reason: reason, ask_reason: reason, ask_price_exact: "20.10", mid_price_exact: "20.10",
        } : {}),
      }),
    })) }],
  })
}

async function installPage(page: Page, unavailable = false, count = 1) {
  let chainRequests = 0
  await page.route("**/api/**", (route) => {
    const url = route.request().url()
    if (url.includes("/covered-calls")) chainRequests++
    return route.fulfill({ json: url.includes("/tickers")
      ? { as_of: "2026-09-11T20:00:00Z", total: 0, results: [] }
      : url.includes("/watchlist") ? { items: [] } : diagnosticPage(unavailable, count) })
  })
  return () => chainRequests
}

for (const width of [320, 390, 768, 1440]) {
  for (const theme of ["light", "dark"] as const) {
    test(`IV disclosure wraps and supports a keyboard at ${width}px in ${theme}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 })
      await page.addInitScript((value) => localStorage.setItem("theme", value), theme)
      const requestCount = await installPage(page)
      await page.goto("/")
      if (width < 640) await page.getByRole("button", { name: /Show details for IREN/ }).first().click()
      const details = page.locator(".iv-details").first()
      const summary = details.locator("summary")
      await expect(summary).toHaveAccessibleName(/IV details for IREN .*call strike.*87.7%/)
      await expect(summary).toContainText("87.7% · Details")
      const before = requestCount()
      await summary.focus()
      await page.keyboard.press("Enter")
      await expect(details).toHaveAttribute("open", "")
      await expect(details).toContainText("72.1%–97.1%")
      await expect(details).toContainText("Years to expiry")
      await expect(details).toContainText("0.01917808219178082191780821918")
      await expect(details).toContainText("Option-chain retrieved (UTC)")
      await expect(details).toContainText("not a statistical confidence interval")
      expect(await summary.evaluate((element) => getComputedStyle(element).outlineStyle)).not.toBe("none")
      expect(await details.evaluate((element) => element.closest("button") === null)).toBe(true)
      expect(await details.evaluate((element) => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1)
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1)
      await page.screenshot({ path: testInfo.outputPath(`iv-details-${width}-${theme}.png`), fullPage: true })
      await page.keyboard.press("Space")
      await expect(details).not.toHaveAttribute("open", "")
      await page.keyboard.press("Enter")
      await expect(details).toHaveAttribute("open", "")
      expect(requestCount()).toBe(before)
      await page.getByRole("button", { name: /Copy row IREN/ }).first().click()
      const copied = await page.evaluate(() => navigator.clipboard.readText())
      expect(copied).toContain("| IV |")
      expect(copied).toContain("IV calculation")
      expect(copied).toContain("Underlying quote time (UTC): 2026-09-11T20:00:00Z")
      expect(copied).toContain("Annual rate (decimal): 0.04")
    })
  }
}

test("unavailable IV exposes the recorded reason without loading more contracts", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 1000 })
  const requestCount = await installPage(page, true, 300)
  await page.goto("/")
  await expect(page.locator(".mobile-option-row")).toHaveCount(250)
  await page.getByRole("button", { name: /Show details for IREN/ }).first().click()
  const details = page.locator(".iv-details").first()
  await expect(details.locator("summary")).toContainText("— · Why unavailable?")
  const before = requestCount()
  await details.locator("summary").click()
  await expect(details).toContainText("Option bid and ask are locked")
  await expect(details).not.toContainText("Quote-implied IV range")
  await expect(page.locator(".mobile-option-row")).toHaveCount(250)
  expect(requestCount()).toBe(before)
  await page.screenshot({ path: testInfo.outputPath("iv-unavailable-mobile.png") })
})

test("put IV retains its midpoint when a zero bid has no implied volatility", async ({ page }) => {
  const reason = { code: "model_bounds" as const, message: "Quoted price must be positive and inside model bounds" }
  await page.route("**/api/**", (route) => route.fulfill({ json: route.request().url().includes("/tickers")
    ? { as_of: "2026-09-11T20:00:00Z", total: 0, results: [] }
    : samplePutPage({ current_cents: 9990, stock_bid_cents: 9990, stock_ask_cents: 10_010,
      fetched_at: "2026-09-11T20:00:01Z", chain_fetched_at: "2026-09-11T20:00:01Z",
      quote_timestamp: "Sep 11, 2026 4:00 PM ET", expirations: [{ expiration: "2026-09-18", dte: 7, contracts: [samplePutContract({
      strike_cents: 10_000, put_bid_cents: 0, put_ask_cents: 20, iv_pct_tenths: 18,
      iv_details: sampleIvDetails({ strike_exact: "100", bid_price_exact: "0", mid_price_exact: "0.10",
        ask_price_exact: "0.20", rate_exact: "0", bid_pct_tenths: null, bid_reason: reason, ask_pct_tenths: 36 }),
    })] }] }) }))
  await page.goto("/?side=put")
  const details = page.locator(".iv-details").first()
  await expect(details.locator("summary")).toHaveAccessibleName(/IV details for IREN .*put strike.*1.8%/)
  await details.locator("summary").click()
  await expect(details).toContainText(reason.message)
  await expect(details).toContainText("Ask IV3.6%")
  await expect(details).not.toContainText("Quote-implied IV range")
  await page.getByRole("button", { name: /Copy row IREN/ }).first().click()
  const copied = await page.evaluate(() => navigator.clipboard.readText())
  expect(copied).toContain("Midpoint IV: 1.8%")
  expect(copied).toContain("Bid IV: Unavailable")
  expect(copied).toContain("Ask IV: 3.6%")
})
