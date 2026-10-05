import { test as base, expect } from "@playwright/test"

// Fixture-only browser evidence: no headers, bodies, credentials or environment dump.
export const test = base.extend({
  page: async ({ page }, runPage, testInfo) => {
    const events: object[] = []
    const pending: Promise<void>[] = []
    const path = (url: string) => { const parsed = new URL(url); return parsed.pathname + parsed.search }
    page.on("request", (request) => {
      if (request.url().includes("/api/")) events.push({ event: "request", path: path(request.url()), time: Date.now() })
    })
    page.on("requestfailed", (request) => {
      if (request.url().includes("/api/")) events.push({ event: "failed", path: path(request.url()), error: request.failure()?.errorText, timing: request.timing() })
    })
    page.on("requestfinished", (request) => {
      if (!request.url().includes("/api/")) return
      pending.push((async () => {
        const response = await request.response()
        events.push({ event: "finished", path: path(request.url()), status: response?.status(), timing: request.timing(), backendTiming: await response?.headerValue("server-timing") })
      })().catch((error) => { events.push({ event: "diagnostic-error", message: String(error) }) }))
    })
    page.on("pageerror", (error) => events.push({ event: "pageerror", message: error.message }))
    page.on("console", (message) => {
      if (["error", "warning"].includes(message.type())) events.push({ event: "console", type: message.type(), message: message.text() })
    })
    await runPage(page)
    if (testInfo.status !== testInfo.expectedStatus) {
      await Promise.allSettled(pending)
      const state = page.isClosed() ? { closed: true } : await page.evaluate(() => ({
        url: location.pathname + location.search,
        heading: document.querySelector("h1")?.textContent,
        context: document.querySelector(".chain-context")?.textContent,
        busy: document.querySelector("main")?.getAttribute("aria-busy"),
        controls: Array.from(document.querySelectorAll('[role="radio"]')).map((node) => ({ text: node.textContent, checked: node.getAttribute("aria-checked") })),
        errors: Array.from(document.querySelectorAll('[role="alert"]')).map((node) => node.textContent),
      })).catch((error) => ({ snapshotError: String(error) }))
      await testInfo.attach("browser-state", { body: JSON.stringify({ state, events }, null, 2), contentType: "application/json" })
    }
  },
})

export { expect }
