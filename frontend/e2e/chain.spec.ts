import { expect, test } from "@playwright/test"

function watchNasdaq(page: import("@playwright/test").Page) {
  const nasdaqHits: string[] = []
  page.on("request", (request) => {
    if (request.url().includes("api.nasdaq.com")) nasdaqHits.push(request.url())
  })
  return nasdaqHits
}

async function chooseTicker(page: import("@playwright/test").Page, symbol: string) {
  const picker = page.getByRole("combobox")
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
  await page.getByRole("radio", { name: "All", exact: true }).click()
  await page.getByText("Columns").click()
  await page.getByRole("checkbox", { name: /IV/ }).click()
  await expect(page.getByRole("checkbox", { name: /IV/ })).toBeChecked()
  await expect(page).toHaveURL(/t=CIFR/)
  await expect(page).toHaveURL(/side=put/)
  await expect(page).toHaveURL(/m=all/)
  await expect(page).toHaveURL(/cols=/)
  await page.reload()
  await expect(page.getByRole("heading", { name: "Cash-secured puts" })).toBeVisible()
  await expect(page.getByRole("radio", { name: "Cash-secured puts" })).toBeChecked()
  await expect(page.getByRole("radio", { name: "All", exact: true })).toBeChecked()
  await expect(page.getByRole("columnheader", { name: "IV" })).toBeVisible()
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
  await page.goto("/")
  const oddsCell = page.locator(".odds-cell").first()
  await expect(oddsCell).toContainText("Odds unavailable")
  await expect(oddsCell.locator(".odds-reason p")).not.toBeVisible()
  await oddsCell.getByText("Why unavailable?").click()
  await expect(oddsCell).toContainText("A coherent underlying bid and ask is unavailable")

  await page.setViewportSize({ width: 390, height: 844 })
  const row = page.locator(".mobile-option-row").first()
  await expect(row.locator(".mobile-row-summary")).toContainText("Odds unavailable")
  await expect(row.locator(".mobile-row-summary")).not.toContainText("A coherent underlying bid and ask is unavailable")
  await row.getByRole("button", { name: /Show details for IREN/ }).click()
  await expect(row.locator(".mobile-row-details")).toContainText("A coherent underlying bid and ask is unavailable")
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
      options.length === 5 && options.every((option) => option.clientHeight >= 44
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
      await expect(page.locator(".mobile-market-quote .market-session-status strong")).toHaveText(/^(Open|Closed|Unavailable)$/)
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
        expect(clipboard).toContain("Cushion (BE)")
      }
      if (viewport.width === 320) {
        await expect(page.locator(".expiry-heading").first().getByText(/contracts$/)).toBeVisible()
        await page.getByRole("button", { name: /Columns/ }).click()
        const picker = page.locator(".column-picker-popover")
        await expect(picker).toBeVisible()
        await expect.poll(() => picker.evaluate((element) => {
          const bounds = element.getBoundingClientRect()
          return bounds.top >= 0 && bounds.bottom <= window.innerHeight
        })).toBe(true)
        await page.keyboard.press("Escape")
      }
    } else {
      await expect(page.getByRole("table")).toBeVisible()
    }
    expect(nasdaqHits).toEqual([])
  })
}
