import { expect, test } from "@playwright/test"
import { sampleContract, sampleIvDetails, samplePage, samplePutContract, samplePutPage } from "../src/testFixtures"

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", (route) => route.fulfill({ json:
    route.request().url().includes("/tickers") ? {
      as_of: "2026-09-11T14:00:00Z", total: 1,
      results: [{ symbol: "IREN", name: "Iris Energy Limited" }],
    } : samplePage({ expirations: [{ expiration: "2026-09-18", dte: 7,
      contracts: [sampleContract({ strike_exact: "123456.789", strike_cents: 12345678,
        call_bid_cents: 1234567, net_apr_pct_tenths: -1234567 })],
    }] }),
  }))
})

for (const width of [320, 390, 448, 449, 639]) {
  test(`mobile financial metrics remain complete at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    await page.goto("/")
    const values = page.locator(".mobile-priority-grid strong")
    await expect(values.first()).toHaveText("$123456.789")
    await page.evaluate(() => document.fonts.ready)
    const clipped = await page.locator(".mobile-priority-grid :is(strong, small)").evaluateAll((nodes) =>
      nodes.filter((node) => node.scrollWidth > node.clientWidth + 1 || node.scrollHeight > node.clientHeight + 1)
        .map((node) => node.textContent))
    expect(clipped).toEqual([])
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    await page.screenshot({ path: testInfo.outputPath(`metrics-${width}.png`) })
  })
}

test("skip link includes the chain title and forecast context", async ({ page }) => {
  await page.goto("/")
  await page.keyboard.press("Tab")
  await page.keyboard.press("Enter")
  const main = page.getByRole("main")
  await expect(main).toBeFocused()
  await expect(main.getByRole("heading", { name: "Covered calls", exact: true })).toBeVisible()
  await expect(main).toContainText("Selected forecast")
})

test("dark put metrics preserve long negative values and complete labels", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 900 })
  await page.addInitScript(() => localStorage.setItem("theme", "dark"))
  await page.route("**/api/cash-secured-puts/**", (route) => route.fulfill({ json: samplePutPage({
    expirations: [{ expiration: "2026-09-18", dte: 7, contracts: [samplePutContract({
      strike_exact: "123456.789", strike_cents: 12345678,
      put_bid_cents: 1234567, net_apr_pct_tenths: -1234567,
    })] }],
  }) }))
  await page.goto("/?side=put")
  await expect(page.getByRole("heading", { name: "Cash-secured puts", exact: true })).toBeVisible()
  await expect(page.locator("html")).toHaveClass(/dark/)
  await expect(page.locator(".mobile-priority-grid strong").first()).toHaveText("$123456.789")
  await expect(page.locator(".mobile-priority-grid")).toContainText("-123456.7%")
  await page.evaluate(() => document.fonts.ready)
  expect(await page.locator(".mobile-priority-grid :is(strong, small)").evaluateAll((nodes) =>
    nodes.filter((node) => node.scrollWidth > node.clientWidth + 1 || node.scrollHeight > node.clientHeight + 1)
      .map((node) => node.textContent))).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320)
  await page.screenshot({ path: testInfo.outputPath("dark-put-metrics.png") })
})

test("chain main landmark contains context and recovery while loading fails", async ({ page }) => {
  let release!: () => void
  const gate = new Promise<void>((resolve) => { release = resolve })
  await page.route("**/api/covered-calls/**", async (route) => {
    await gate
    await route.fulfill({ status: 503, json: { detail: "Recorded provider outage" } })
  })
  try {
    await page.goto("/")
    const main = page.getByRole("main")
    await expect(main).toHaveCount(1)
    await expect(main).toHaveAttribute("aria-busy", "true")
    await expect(main.getByRole("heading", { name: "Covered calls", exact: true })).toBeVisible()
    await expect(main.getByRole("status").filter({ hasText: "Loading IREN market data" })).toBeVisible()
    await page.keyboard.press("Tab")
    await page.keyboard.press("Enter")
    await expect(main).toBeFocused()
  } finally { release() }
  const main = page.getByRole("main")
  await expect(main).toHaveAttribute("aria-busy", "false")
  await expect(main.getByRole("alert")).toContainText("Recorded provider outage")
  await expect(main.getByRole("button", { name: "Try again", exact: true })).toBeVisible()
  await expect(main).toBeFocused()
})

test("touch controls and narrow form text remain usable", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true })
  const page = await context.newPage()
  await page.route("**/api/**", (route) => route.fulfill({ json: route.request().url().includes("/tickers")
    ? { as_of: "2026-09-11T14:00:00Z", total: 1, results: [{ symbol: "IREN", name: "Iris Energy Limited" }] } : samplePage() }))
  try {
    await page.goto(baseURL!)
    for (const control of [page.getByRole("link", { name: "Option chain", exact: true }),
      page.getByRole("button", { name: /^Theme:/ }), page.getByRole("button", { name: "Settings", exact: true }),
      page.getByRole("button", { name: "Filters", exact: true })]) {
      const box = await control.boundingBox()
      expect(box!.height).toBeGreaterThanOrEqual(44)
      expect(box!.width).toBeGreaterThanOrEqual(44)
    }
    await page.getByRole("button", { name: "Settings", exact: true }).click()
    for (const input of [page.getByRole("combobox", { name: "Ticker" }), page.getByRole("textbox", { name: "Contracts" })]) {
      expect(await input.evaluate((node) => parseFloat(getComputedStyle(node).fontSize))).toBeGreaterThanOrEqual(16)
      expect((await input.boundingBox())!.height).toBeGreaterThanOrEqual(44)
    }
    await page.getByRole("combobox", { name: "Ticker" }).tap()
    expect((await page.getByRole("option", { name: /Iris Energy Limited/ }).boundingBox())!.height).toBeGreaterThanOrEqual(44)
    await page.keyboard.press("Escape")
    await page.keyboard.press("Escape")
    await expect(page.getByRole("button", { name: "Settings", exact: true })).toBeFocused()
    await page.getByRole("button", { name: "Filters", exact: true }).click()
    expect(await page.getByLabel("Min IV (%)", { exact: true }).evaluate((node) => parseFloat(getComputedStyle(node).fontSize))).toBeGreaterThanOrEqual(16)
  } finally { await context.close() }
})

for (const width of [768, 1024]) {
  test(`touch targets remain usable across the tablet sidebar breakpoint at ${width}px`, async ({ browser, baseURL }) => {
    const context = await browser.newContext({ viewport: { width, height: 900 }, hasTouch: true })
    const page = await context.newPage()
    await page.route("**/api/**", (route) => route.fulfill({ json: route.request().url().includes("/tickers")
      ? { as_of: "2026-09-11T14:00:00Z", total: 0, results: [] } : samplePage() }))
    try {
      await page.goto(baseURL!)
      await page.getByRole("button", { name: "Filters", exact: true }).tap()
      if (width < 1024) await page.getByRole("button", { name: "Settings", exact: true }).tap()
      const targets = page.locator('button:visible:not(:disabled), [role="radio"]:visible, select:visible, summary:visible, .workspace-nav a:visible')
      await expect(targets.first()).toBeVisible()
      const undersized = await targets.evaluateAll((nodes) => nodes.flatMap((node) => {
        const { width, height } = node.getBoundingClientRect()
        return width < 44 || height < 44 ? [{ label: node.getAttribute("aria-label") ?? node.textContent, width, height }] : []
      }))
      expect(undersized).toEqual([])
      if (width <= 768) {
        expect(await page.getByRole("combobox", { name: "Ticker" }).evaluate((node) => parseFloat(getComputedStyle(node).fontSize)))
          .toBeGreaterThanOrEqual(16)
      }
      if (width < 1024) {
        await page.getByRole("button", { name: "Close settings", exact: true }).tap()
        await expect(page.getByRole("button", { name: "Settings", exact: true })).toBeFocused()
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    } finally { await context.close() }
  })
}

test("coarse-pointer model dialog keeps nested evidence and closing controls usable", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ viewport: { width: 320, height: 900 }, hasTouch: true })
  const page = await context.newPage()
  await page.route("**/api/**", (route) => route.fulfill({ json: route.request().url().includes("/watchlist")
    ? { items: [{
      id: "recorded", ticker: "IREN", root: "IREN", side: "call", expiration: "2026-09-18",
      strike_exact: "48.000", terms_note: "Standard terms", created_at: "2026-09-11T14:00:00Z",
      outcome: { status: "pending", reason: "Expiry trading session has not completed" },
      physical_models: [{ method: "lognormal_ewma", status: "available", itm_pct_tenths: 610,
        otm_pct_tenths: 390, atm_pct_tenths: 0, model_evidence: { prospective: {
          ticker_origin_horizon_units: 500, independent_date_blocks: 20,
          rejection_reasons: { rights_cleared_option_history_unavailable: 10 },
        } },
      }],
    }] }
    : { as_of: "2026-09-11T14:00:00Z", total: 0, results: [] } }))
  try {
    await page.goto(`${baseURL}/watchlist`)
    const trigger = page.getByRole("button", { name: "Compare models", exact: true })
    await trigger.tap()
    const dialog = page.getByRole("dialog", { name: "Compare models", exact: true })
    await expect(dialog).toBeVisible()
    await dialog.locator(".model-result-heading").tap()
    const targets = dialog.locator("button:visible, summary:visible")
    expect(await targets.evaluateAll((nodes) => nodes.flatMap((node) => {
      const { width, height } = node.getBoundingClientRect()
      return width < 44 || height < 44 ? [{ label: node.textContent, width, height }] : []
    }))).toEqual([])
    await dialog.getByText(/Prospective as-issued ·/).tap()
    const evidence = dialog.getByRole("region", { name: "Prospective as-issued evidence" })
    await expect(evidence).toBeVisible()
    expect(await dialog.evaluate((node) => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1)
    await dialog.getByRole("button", { name: "Close model comparison", exact: true }).tap()
    await expect(dialog).toBeHidden()
    await expect(trigger).toBeFocused()
  } finally { await context.close() }
})

test("mobile inline IV disclosure has a usable touch target", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true })
  const page = await context.newPage()
  await page.route("**/api/**", (route) => route.fulfill({ json: samplePage({
    expirations: [{ expiration: "2026-09-18", dte: 7, contracts: [sampleContract({
      iv_pct_tenths: 877, iv_details: sampleIvDetails(),
    })] }],
  }) }))
  try {
    await page.goto(baseURL!)
    await page.getByRole("button", { name: /^Show details for/ }).first().tap()
    const disclosure = page.locator(".mobile-iv-details summary").first()
    await expect(disclosure).toBeVisible()
    const box = await disclosure.boundingBox()
    expect(box!.height).toBeGreaterThanOrEqual(44)
    expect(box!.width).toBeGreaterThanOrEqual(44)
    await disclosure.tap()
    await expect(page.locator(".mobile-iv-details .iv-details").first()).toHaveAttribute("open", "")
    await expect(page.locator(".mobile-iv-details .iv-facts").first()).toBeVisible()
    await disclosure.tap()
    await expect(page.locator(".mobile-iv-details .iv-facts").first()).toBeHidden()
  } finally { await context.close() }
})
