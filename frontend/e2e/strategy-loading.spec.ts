import { expect, test } from "./diagnostics"

import { samplePage, samplePutPage } from "../src/testFixtures"

for (const width of [390, 1024, 1280]) {
  for (const outcome of ["ready", "error"] as const) {
    test(`quote ${outcome} preserves a pressed strategy control at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 900 })
      let release!: () => void
      const gate = new Promise<void>((resolve) => { release = resolve })
      const requests: string[] = []
      const stamp = "Sep 11, 2026 10:00 AM ET · Recorded quote timestamp with several lines of context"
      await page.route("**/api/**", async (route) => {
        const url = new URL(route.request().url())
        requests.push(url.pathname + url.search)
        if (url.pathname === "/api/tickers") {
          return route.fulfill({ json: {
            as_of: "2026-09-11T14:00:00Z", total: 1,
            results: [{ symbol: "CIFR", name: "Cipher Mining Inc." }],
          } })
        }
        if (url.pathname === "/api/covered-calls/CIFR") {
          await gate
          if (outcome === "error") {
            return route.fulfill({ status: 503, json: { detail: "Recorded fixture outage" } })
          }
        }
        const ticker = url.pathname.endsWith("/CIFR") ? "CIFR" : "IREN"
        const overrides = { ticker, name: ticker === "CIFR" ? "Cipher Mining Inc." : "Iris Energy Limited", quote_timestamp: stamp }
        return route.fulfill({ json: url.pathname.includes("/cash-secured-puts/")
          ? samplePutPage(overrides) : samplePage(overrides) })
      })
      try {
        await page.goto("/")
        if (width < 1024) await page.getByRole("button", { name: "Settings", exact: true }).click()
        await expect(page.getByRole("region", { name: "IREN market summary" })).toHaveAttribute("aria-busy", "false")
        const picker = page.getByRole("combobox", { name: "Ticker" })
        await picker.click()
        await picker.fill("CIFR")
        await page.getByRole("option", { name: /CIFR/ }).click()
        await expect(page).toHaveURL(/t=CIFR/)
        const quote = page.getByRole("region", { name: "CIFR market summary" })
        await expect(quote).toHaveAttribute("aria-busy", "true")
        const radio = page.getByRole("radio", { name: "Cash-secured puts", exact: true })
        await radio.scrollIntoViewIfNeeded()
        const before = (await radio.boundingBox())!
        await page.mouse.move(before.x + before.width / 2, before.y + before.height / 2)
        await page.mouse.down()
        release()
        await expect(quote).toHaveAttribute("aria-busy", "false")
        if (outcome === "ready") await expect(quote).toContainText(stamp)
        else await expect(page.locator(".chain-panel [role=alert]")).toContainText("Recorded fixture outage")
        const after = (await radio.boundingBox())!
        await page.mouse.up()
        await testInfo.attach("pointer-geometry", { body: JSON.stringify({ before, after, requests }), contentType: "application/json" })
        expect(Math.abs(after.y - before.y)).toBeLessThanOrEqual(0.5)
        await expect(radio).toBeChecked()
        await expect(page).toHaveURL(/t=CIFR/)
        await expect(page).toHaveURL(/side=put/)
        await expect.poll(() => requests.some((url) => url.startsWith("/api/cash-secured-puts/CIFR?"))).toBe(true)
        await expect(quote).toHaveAttribute("aria-busy", "false")
        await expect(quote).toContainText(stamp)
        await page.screenshot({ path: testInfo.outputPath(`strategy-${width}-${outcome}.png`) })
      } finally {
        release()
        await page.mouse.up()
      }
    })
  }
}
