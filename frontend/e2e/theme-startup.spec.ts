import { expect, test } from "@playwright/test"

for (const colorScheme of ["dark", "light"] as const) {
  for (const preference of ["denied", "missing", "invalid", "dark", "light"] as const) {
    test(`theme bootstrap: ${colorScheme} system with ${preference} storage`, async ({ browser, baseURL }) => {
      const context = await browser.newContext({ baseURL, colorScheme })
      try {
        await context.addInitScript((stored) => {
          if (stored === "denied") {
            Object.defineProperty(window, "localStorage", {
              get() { throw new DOMException("Storage denied", "SecurityError") },
            })
          } else if (stored !== "missing") {
            localStorage.setItem("theme", stored)
          }
        }, preference)
        const page = await context.newPage()
        // Verify the pre-paint bootstrap itself, before React can run.
        await page.route("**/src/main.tsx*", (route) => route.abort())
        await page.goto("/", { waitUntil: "domcontentloaded" })
        const expectedDark = preference === "dark"
          || (preference !== "light" && colorScheme === "dark")
        expect(await page.locator("html").evaluate((node) => node.classList.contains("dark")))
          .toBe(expectedDark)
        await expect(page.locator("#root")).toBeEmpty()
      } finally {
        await context.close()
      }
    })
  }
}
