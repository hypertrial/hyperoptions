import { expect, test } from "@playwright/test"
import { samplePage } from "../src/testFixtures"

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", (route) => route.fulfill({ json:
    route.request().url().includes("/tickers") ? {
      as_of: "2026-09-11T14:00:00Z", total: 10,
      results: Array.from({ length: 10 }, (_, index) => ({ symbol: `A${String.fromCharCode(65 + index)}`, name: `Recorded company ${index}` })),
    } : route.request().url().includes("/watchlist") ? { items: [] } : samplePage(),
  }))
})

test("ticker keyboard and pointer selection have one announced popup", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto("/")
  const input = page.getByRole("combobox", { name: "Ticker" })
  await input.focus()
  await expect(page.getByRole("option", { name: /Recorded company 0/ })).toBeVisible()
  await input.fill("iren")
  await expect(page.getByRole("option", { name: /Recorded company 0/ })).toBeVisible()
  await expect(input).toHaveAttribute("aria-expanded", "true")
  await page.keyboard.press("Escape")
  await expect(input).toHaveAttribute("aria-expanded", "false")
  await page.keyboard.press("End")
  expect(await input.evaluate((element: HTMLInputElement) => element.selectionStart)).toBe(4)
  await page.keyboard.press("Home")
  expect(await input.evaluate((element: HTMLInputElement) => element.selectionStart)).toBe(0)
  await page.keyboard.press("Enter")
  await expect(page).not.toHaveURL(/t=AA/)
  await page.keyboard.press("ArrowDown")
  const first = page.getByRole("option", { name: /Recorded company 0/ })
  await expect(input).toHaveAttribute("aria-activedescendant", (await first.getAttribute("id"))!)
  await page.keyboard.press("Escape")
  await page.keyboard.press("ArrowUp")
  const last = page.getByRole("option", { name: /Recorded company 9/ })
  await expect(input).toHaveAttribute("aria-activedescendant", (await last.getAttribute("id"))!)
  await expect(last).toBeInViewport()
  await page.keyboard.press("Enter")
  await expect(page).toHaveURL(/t=AJ/)
  await expect(input).toBeFocused()
  await input.click()
  await first.click()
  await expect(page).toHaveURL(/t=AA/)
  await expect(input).toBeFocused()
  await expect(input).toHaveAttribute("aria-expanded", "false")
  await input.blur()
  await input.focus()
  await expect(input).toHaveAttribute("aria-expanded", "true")
})

test("watchlist exposes a usable main landmark while its route chunk loads", async ({ page }) => {
  let release!: () => void
  const gate = new Promise<void>((resolve) => { release = resolve })
  await page.route("**/src/watchlist/Watchlist.tsx", async (route) => {
    await gate
    await route.continue()
  })
  try {
    await page.goto("/watchlist")
    await expect(page.getByText("Loading watchlist…")).toBeVisible()
    await expect(page.getByRole("main")).toHaveAttribute("id", "main-content")
    await expect(page.getByRole("status").filter({ hasText: "Loading watchlist…" })).toBeVisible()
    await page.keyboard.press("Tab")
    await expect(page.getByRole("link", { name: "Skip to main content" })).toBeFocused()
    await page.keyboard.press("Enter")
    await expect(page.getByRole("main")).toBeFocused()
  } finally { release() }
  await expect(page.getByRole("heading", { name: "Watchlist", exact: true })).toBeVisible()
})

test("ticker suggestions dismiss before the settings drawer and support touch selection", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true })
  const page = await context.newPage()
  await page.route("**/api/**", (route) => route.fulfill({ json: route.request().url().includes("/tickers")
    ? { as_of: "2026-09-11T14:00:00Z", total: 1, results: [{ symbol: "CIFR", name: "Cipher Mining" }] }
    : samplePage() }))
  try {
    await page.goto("http://127.0.0.1:5173/")
    await page.getByRole("button", { name: "Settings", exact: true }).click()
    const input = page.getByRole("combobox", { name: "Ticker" })
    await input.tap()
    await expect(page.getByRole("option", { name: /CIFR/ })).toBeVisible()
    await page.keyboard.press("Escape")
    await expect(input).toHaveAttribute("aria-expanded", "false")
    await expect(page.locator(".drawer-popup")).toBeVisible()
    await input.tap()
    await page.getByRole("option", { name: /CIFR/ }).tap()
    await expect(page).toHaveURL(/t=CIFR/)
    await expect(input).toBeFocused()
    await expect(input).toHaveAttribute("aria-expanded", "false")
    await page.keyboard.press("Escape")
    await expect(page.locator(".drawer-popup")).toBeHidden()
    await expect(page.getByRole("button", { name: "Settings", exact: true })).toBeFocused()
  } finally { await context.close() }
})

for (const width of [390, 1440]) {
  test(`clipboard denial is visible and retryable at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    await page.addInitScript(() => {
      let attempts = 0
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
        writeText: async () => { if (++attempts <= 2) throw new DOMException("Denied", "NotAllowedError") },
      } })
    })
    await page.goto("/")
    if (width < 640) await page.getByRole("button", { name: /Show details for IREN/ }).first().click()
    const button = page.getByRole("button", { name: /Copy row IREN/ }).first()
    await button.click()
    await expect(page.getByRole("alert")).toContainText("Allow clipboard access")
    await expect(page.getByRole("alert")).toBeInViewport()
    await page.screenshot({ path: testInfo.outputPath(`copy-error-${width}.png`) })
    await page.getByRole("button", { name: "Dismiss", exact: true }).click()
    await expect(page.getByRole("alert")).toHaveCount(0)
    await button.click()
    await expect(page.getByRole("alert")).toBeInViewport()
    await button.click()
    await expect(page.getByRole("alert")).toHaveCount(0)
    await expect(page.getByRole("status").filter({ hasText: /^Copied$/ })).toHaveCount(1)
  })
}

test("expanded model evidence wraps long reason codes on narrow phones", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 900 })
  await page.route("**/api/watchlist**", (route) => route.fulfill({ json: { items: [{
    id: "recorded", ticker: "IREN", root: "IREN", side: "call", expiration: "2026-09-18",
    strike_exact: "48.000", terms_note: "Standard terms", created_at: "2026-09-11T14:00:00Z",
    outcome: { status: "pending", reason: "Expiry trading session has not completed" },
    physical_models: [{ method: "lognormal_ewma", status: "available", itm_pct_tenths: 610,
      otm_pct_tenths: 390, atm_pct_tenths: 0, model_evidence: { prospective: {
        ticker_origin_horizon_units: 500, independent_date_blocks: 20,
        rejection_reasons: { rights_cleared_option_history_unavailable: 10 },
      } },
    }],
  }] } }))
  await page.goto("/watchlist")
  await page.getByRole("button", { name: "Compare models" }).click()
  await page.locator(".model-result-heading").click()
  await page.getByText(/Prospective as-issued ·/).click()
  await page.evaluate(() => document.fonts.ready)
  const evidence = page.getByRole("region", { name: "Prospective as-issued evidence" })
  const overflow = await evidence.evaluate((element) => element.scrollWidth - element.clientWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  await page.screenshot({ path: testInfo.outputPath("narrow-evidence.png") })
  await page.keyboard.press("Escape")
  await expect(page.getByRole("button", { name: "Compare models" })).toBeFocused()
})
