import { expect, test } from "@playwright/test"

for (const width of [320, 390]) {
  test(`keeps expiry dates on one line at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 })
    await page.goto("/")
    const heading = page.locator(".expiry-heading").first()
    await expect(heading).toBeVisible()
    const lines = await heading.locator(".expiry-trigger > span").first().evaluate((span) => {
      const range = document.createRange()
      range.selectNodeContents(span)
      return range.getClientRects().length
    })
    expect(lines).toBe(1)
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
  })
}
