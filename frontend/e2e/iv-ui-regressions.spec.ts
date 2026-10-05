import { expect, test, type Page } from "@playwright/test"

import { sampleContract, sampleIvDetails, samplePage } from "../src/testFixtures"

function quotedPage() {
  return samplePage({
    current_cents: 10_000,
    expirations: [{ expiration: "2026-09-18", dte: 7, contracts: [80, 85].map((strike) => sampleContract({
      strike_cents: strike * 100,
      strike_exact: strike.toFixed(3),
      call_bid_cents: (100 - strike) * 100 + 10,
      call_ask_cents: (100 - strike) * 100 + 30,
      iv_pct_tenths: 877,
      iv_details: sampleIvDetails({
        strike_exact: strike.toFixed(3),
        bid_price_exact: (100 - strike + .1).toFixed(2),
        mid_price_exact: (100 - strike + .2).toFixed(2),
        ask_price_exact: (100 - strike + .3).toFixed(2),
      }),
    })) }],
  })
}

async function installApi(page: Page, payload = quotedPage()) {
  let requests = 0
  await page.route("**/api/**", (route) => {
    const url = route.request().url()
    if (url.includes("/covered-calls")) requests++
    return route.fulfill({ json: url.includes("/tickers")
      ? { as_of: "2026-09-11T20:00:00Z", total: 0, results: [] }
      : url.includes("/watchlist") ? { items: [] } : payload })
  })
  return () => requests
}

function ivSummary(page: Page, strike = "85.000") {
  return page.getByLabel(`IV details for IREN 2026-09-18 call strike $${strike}: 87.7% · Details`)
}

const openPanels = (page: Page) => page.locator(".iv-details-content:popover-open")

async function recordIvDismissalEvents(page: Page) {
  await page.evaluate(() => {
    const recorded: unknown[] = []
    ;(window as unknown as { ivDismissalEvents: unknown[] }).ivDismissalEvents = recorded
    const state = () => [...document.querySelectorAll<HTMLDetailsElement>(".iv-details")].map((details) => ({
      label: details.querySelector("summary")?.getAttribute("aria-label"),
      detailsOpen: details.open,
      popoverOpen: details.querySelector("[popover]")?.matches(":popover-open"),
    }))
    const append = (value: unknown) => { if (recorded.length < 1_000) recorded.push(value) }
    for (const type of ["pointerdown", "pointerup", "pointercancel", "mousedown", "mouseup", "click", "keydown", "beforetoggle", "toggle"]) {
      for (const capture of [true, false]) document.addEventListener(type, (event) => {
        const toggle = event as Event & { oldState?: string; newState?: string; source?: Element }
        const target = event.target instanceof Element ? event.target : null
        append({
          type, phase: capture ? "capture" : "bubble", target: target?.tagName,
          oldState: toggle.oldState, newState: toggle.newState,
          source: toggle.source?.getAttribute("aria-label"), state: state(),
        })
      }, capture)
    }
    for (const details of document.querySelectorAll(".iv-details")) {
      new MutationObserver(() => append({ type: "details-mutation", state: state() }))
        .observe(details, { attributes: true, attributeFilter: ["open"] })
    }
  })
}

test.afterEach(async ({ page }, info) => {
  if (info.status === info.expectedStatus) return
  const events = await page.evaluate(() => (window as unknown as { ivDismissalEvents?: unknown[] }).ivDismissalEvents)
  if (events) await info.attach("iv-dismissal-events", { body: JSON.stringify(events, null, 2), contentType: "application/json" })
})

test("desktop IV uses a native popover without resizing the table and restores focus", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const requests = await installApi(page)
  await page.goto("/")
  const summary = ivSummary(page)
  await summary.scrollIntoViewIfNeeded()
  await recordIvDismissalEvents(page)
  const table = page.getByRole("table")
  const before = await table.boundingBox()
  const requestCount = requests()
  await summary.click()
  const panel = page.getByRole("region", { name: "IV calculation for IREN 2026-09-18 call strike $85.000", exact: true })
  await expect(panel).toBeVisible()
  await expect(panel).toHaveAttribute("popover", "auto")
  await expect(openPanels(page)).toHaveCount(1)
  const after = await table.boundingBox()
  expect(Math.abs(after!.width - before!.width)).toBeLessThanOrEqual(1)
  expect(Math.abs(after!.height - before!.height)).toBeLessThanOrEqual(1)
  const bounds = await panel.boundingBox()
  expect(bounds!.x).toBeGreaterThanOrEqual(0)
  expect(bounds!.y).toBeGreaterThanOrEqual(0)
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(1440)
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(900)

  // A taller desktop leaves the trigger above the centered top-layer panel.
  await page.setViewportSize({ width: 1440, height: 1800 })
  const exposedTrigger = await summary.boundingBox()
  const loweredPanel = await panel.boundingBox()
  expect(exposedTrigger!.y + exposedTrigger!.height).toBeLessThan(loweredPanel!.y)

  await summary.click()
  await expect(openPanels(page)).toHaveCount(0)
  await summary.click()
  await expect(openPanels(page)).toHaveCount(1)
  await panel.getByRole("button", { name: "Close", exact: true }).click()
  await expect(openPanels(page)).toHaveCount(0)
  await expect(summary).toBeFocused()

  await summary.press("Enter")
  await expect(openPanels(page)).toHaveCount(1)
  await summary.press("Space")
  await expect(openPanels(page)).toHaveCount(0)
  await summary.press("Space")
  await expect(openPanels(page)).toHaveCount(1)
  await summary.press("Escape")
  await expect(openPanels(page)).toHaveCount(0)
  await expect(summary).toBeFocused()
  await summary.press("Enter")
  await expect(openPanels(page)).toHaveCount(1)
  await panel.focus()
  await panel.press("Escape")
  await expect(openPanels(page)).toHaveCount(0)
  await expect(summary).toBeFocused()

  await summary.press("Enter")
  await expect(openPanels(page)).toHaveCount(1)
  const theme = page.getByRole("button", { name: /^Theme:/ })
  await theme.click()
  await expect(openPanels(page)).toHaveCount(0)
  await expect(theme).toBeFocused()
  expect(requests()).toBe(requestCount)
})

test("an IV trigger click closes when native dismissal precedes its click event", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await installApi(page)
  await page.goto("/")
  const summary = ivSummary(page)
  await summary.scrollIntoViewIfNeeded()
  await recordIvDismissalEvents(page)
  await summary.click()
  await expect(openPanels(page)).toHaveCount(1)
  await page.setViewportSize({ width: 1440, height: 1800 })
  const trigger = await summary.boundingBox()
  const panel = await openPanels(page).boundingBox()
  expect(trigger!.y + trigger!.height).toBeLessThan(panel!.y)

  await page.mouse.move(trigger!.x + trigger!.width / 2, trigger!.y + trigger!.height / 2)
  await page.mouse.down()
  // Chromium may dismiss at mousedown or pointerup. Exercise the observed
  // ordering independently of which native event performs light dismissal.
  await page.locator(".iv-details-content[popover]").evaluateAll((panels) => {
    for (const panel of panels) if (panel.matches(":popover-open")) (panel as HTMLElement).hidePopover()
  })
  await expect(openPanels(page)).toHaveCount(0)
  await expect(summary.locator("..")).not.toHaveAttribute("open", "")
  await page.mouse.up()
  await expect(openPanels(page)).toHaveCount(0)
  await expect(summary.locator("..")).not.toHaveAttribute("open", "")
  await expect(summary).toBeFocused()

  await summary.click()
  await expect(openPanels(page)).toHaveCount(1)
  await openPanels(page).getByRole("button", { name: "Close", exact: true }).click()
  await expect(openPanels(page)).toHaveCount(0)
  await expect(summary).toBeFocused()
})

test("a canceled IV trigger pointer gesture leaves keyboard activation usable", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await installApi(page)
  await page.goto("/")
  const summary = ivSummary(page)
  await summary.scrollIntoViewIfNeeded()
  await recordIvDismissalEvents(page)
  await summary.click()
  await expect(openPanels(page)).toHaveCount(1)
  await page.setViewportSize({ width: 1440, height: 1800 })
  const trigger = await summary.boundingBox()
  await page.mouse.move(trigger!.x + trigger!.width / 2, trigger!.y + trigger!.height / 2)
  await page.mouse.down()
  await summary.dispatchEvent("pointercancel", { pointerId: 1, pointerType: "mouse", isPrimary: true })
  await page.locator(".iv-details-content[popover]").evaluateAll((panels) => {
    for (const panel of panels) if (panel.matches(":popover-open")) (panel as HTMLElement).hidePopover()
  })
  await page.mouse.move(1, 1)
  await page.mouse.up()
  await expect(openPanels(page)).toHaveCount(0)
  await summary.press("Enter")
  await expect(openPanels(page)).toHaveCount(1)
  await expect(summary.locator("..")).toHaveAttribute("open", "")
  await summary.press("Space")
  await expect(openPanels(page)).toHaveCount(0)
  await expect(summary.locator("..")).not.toHaveAttribute("open", "")
})

test("opening another contract replaces the popover and its recorded strike", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await installApi(page)
  await page.goto("/")
  const first = ivSummary(page)
  const second = ivSummary(page, "80.000")
  await first.press("Enter")
  await expect(openPanels(page)).toContainText("call strike $85.000")
  await second.press("Enter")
  await expect(openPanels(page)).toHaveCount(1)
  await expect(openPanels(page)).toContainText("call strike $80.000")
  await expect(first.locator("..")).not.toHaveAttribute("open", "")
  await expect(second.locator("..")).toHaveAttribute("open", "")
})

for (const change of ["collapse", "filter", "navigation", "resize"] as const) {
  test(`an open IV popover does not survive ${change} of its contract view`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await installApi(page)
    await page.goto("/")
    if (change === "filter") await page.getByRole("button", { name: "Filters", exact: true }).click()
    await ivSummary(page).press("Enter")
    await expect(openPanels(page)).toHaveCount(1)
    if (change === "collapse") {
      const collapse = page.getByRole("button", { name: "Collapse all", exact: true })
      await collapse.focus()
      await expect(openPanels(page)).toHaveCount(1)
      await collapse.press("Enter")
      await expect(page.getByRole("table")).toHaveCount(0)
    } else if (change === "filter") {
      await page.getByLabel("Min IV (%)", { exact: true }).fill("90")
      await expect(page.getByText("No rows match the current filters.")).toBeVisible()
    } else if (change === "navigation") {
      const watchlist = page.getByRole("link", { name: "Watchlist", exact: true })
      await watchlist.focus()
      await expect(openPanels(page)).toHaveCount(1)
      await watchlist.press("Enter")
      await expect(page.getByRole("heading", { name: "Watchlist", exact: true })).toBeVisible()
    } else {
      await page.setViewportSize({ width: 320, height: 844 })
      await expect(page.locator(".mobile-option-row")).toHaveCount(2)
      await page.locator(".mobile-option-row").first().getByRole("button", { name: /Show details for/ }).click()
      await expect(page.locator(".mobile-iv-details .iv-details").first()).not.toHaveAttribute("open", "")
      await page.setViewportSize({ width: 1440, height: 900 })
      await expect(ivSummary(page).locator("..")).not.toHaveAttribute("open", "")
    }
    await expect(openPanels(page)).toHaveCount(0)
    await expect(page.locator(".iv-details[open]")).toHaveCount(0)
  })
}

for (const path of ["/", "/watchlist", "/missing-workstation-page"]) {
  test(`global Theme and keyboard skip link remain usable on ${path}`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.emulateMedia({ colorScheme: "light" })
    await page.addInitScript(() => localStorage.setItem("theme", "system"))
    await installApi(page)
    await page.goto(path)
    await expect(page.getByRole("main")).toBeVisible()
    if (path === "/watchlist") await expect(page.getByRole("heading", { name: "Watchlist", exact: true })).toBeVisible()
    await page.keyboard.press("Tab")
    await expect(page.getByRole("link", { name: "Skip to main content" })).toBeFocused()
    await page.keyboard.press("Enter")
    await expect(page.getByRole("main")).toBeFocused()
    const url = page.url()
    await expect(page.getByRole("button", { name: /^Theme:/ })).toHaveCount(1)
    const theme = page.getByRole("button", { name: "Theme: system", exact: true })
    await expect(theme).toBeVisible()
    await theme.press("Enter")
    await expect(page.locator("html")).toHaveClass(/dark/)
    await expect(page.getByRole("button", { name: "Theme: dark", exact: true })).toBeVisible()
    await page.getByRole("button", { name: "Theme: dark", exact: true }).press("Enter")
    await expect(page.locator("html")).not.toHaveClass(/dark/)
    await expect(page.getByRole("button", { name: "Theme: light", exact: true })).toBeVisible()
    expect(await page.evaluate(() => localStorage.getItem("theme"))).toBe("light")
    expect(page.url()).toBe(url)
  })
}

test("a complete large premium fits expanded contract details at 320 pixels", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 })
  await installApi(page, samplePage({ expirations: [{ expiration: "2026-09-18", dte: 7,
    contracts: [sampleContract({ net_premium_cents: 123_456_789 })],
  }] }))
  await page.goto("/")
  const row = page.locator(".mobile-option-row").first()
  await row.getByRole("button", { name: /Show details for/ }).click()
  const premium = row.locator(".mobile-row-details > dl > div").filter({ has: page.getByText("Premium (net)", { exact: true }) }).locator("dd")
  await expect(premium).toHaveText("$1,234,567.89")
  await expect(premium).toBeVisible()
  expect(await premium.evaluate((value) => {
    const bounds = value.getBoundingClientRect()
    const text = value.firstChild!
    return Array.from(value.textContent ?? "").every((_character, index) => {
      const range = document.createRange()
      range.setStart(text, index)
      range.setEnd(text, index + 1)
      const character = range.getBoundingClientRect()
      return character.left >= bounds.left - 1 && character.right <= bounds.right + 1
        && character.top >= bounds.top - 1 && character.bottom <= bounds.bottom + 1
    }) && value.scrollWidth <= value.clientWidth + 1 && getComputedStyle(value).textOverflow !== "ellipsis"
  })).toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1)
})
