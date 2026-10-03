// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { MemoryRouter } from "react-router-dom"
import { fetchChain, fetchTickers } from "./api"
import type { Job, WatchItem } from "./generated/types.gen"
import ItmChain from "./ItmChain"
import { samplePage } from "./testFixtures"
import { addWatch } from "./watchlist/api"
import Watchlist from "./watchlist/Watchlist"

vi.mock("./api", async original => ({
  ...await original<typeof import("./api")>(), fetchChain: vi.fn(), fetchTickers: vi.fn(),
}))
vi.mock("./watchlist/api", async original => ({
  ...await original<typeof import("./watchlist/api")>(), addWatch: vi.fn(),
}))

const job: Job = { id: "job1", kind: "watch_refresh", state: "running", progress: 0.5, message: "Checking" }
const item: WatchItem = {
  id: "watch1", ticker: "IREN", root: "IREN", side: "call", expiration: "2026-09-18",
  strike_exact: "50.000", terms_note: "Standard 100-share terms", created_at: "2026-09-11T14:00:00Z",
  outcome: { status: "pending", reason: "Expiry trading session has not completed" },
}
const json = (value: unknown) => new Response(JSON.stringify(value))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

for (const denied of [false, true]) {
  it(`loads the watchlist when storage reads are denied=${denied}`, async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ items: [] })))
    if (denied) vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("Storage disabled", "SecurityError")
    })
    render(<MemoryRouter><Watchlist /></MemoryRouter>)
    expect(await screen.findByText("No watched contracts yet")).toBeTruthy()
    expect(screen.queryByRole("alert")).toBeNull()
  })
}

it("keeps a successful active-job load and polls in memory when storage is full", async () => {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("Storage full", "QuotaExceededError")
  })
  const fetch = vi.fn(async (url: string) => json(url.includes("/api/jobs/") ? job : { items: [], active_job: job }))
  vi.stubGlobal("fetch", fetch)
  render(<MemoryRouter><Watchlist /></MemoryRouter>)
  expect(await screen.findByText("No watched contracts yet")).toBeTruthy()
  expect(await screen.findByRole("progressbar", { name: "Watchlist refresh progress" })).toBeTruthy()
  await waitFor(() => expect(fetch.mock.calls.some(([url]) => url === "/api/jobs/job1")).toBe(true))
  expect(screen.queryByRole("alert")).toBeNull()
})

for (const state of ["succeeded", "failed", "missing"] as const) {
  it(`clears in-memory polling after a ${state} job when storage removal throws`, async () => {
    sessionStorage.setItem("hyperoptions.watchlist.job", job.id)
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("Storage disabled", "SecurityError")
    })
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (!url.includes("/api/jobs/")) return json({ items: [] })
      if (state === "missing") return new Response(JSON.stringify({ detail: "Job not found" }), { status: 404 })
      return json({ ...job, state, error: state === "failed" ? "Provider unavailable" : null })
    }))
    render(<MemoryRouter><Watchlist /></MemoryRouter>)
    const notice = state === "succeeded" ? "Expiry results checked." : state === "failed" ? "Provider unavailable" : "Job not found"
    expect(await screen.findByText(notice)).toBeTruthy()
    expect((screen.getByRole("button", { name: "Check expiry results" }) as HTMLButtonElement).disabled).toBe(false)
    expect(screen.queryByRole("alert")).toBeNull()
  })
}

it("completes a manual refresh when job persistence fails", async () => {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("Storage full", "QuotaExceededError")
  })
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url === "/api/watchlist/refresh") return json({ job })
    if (url === "/api/jobs/job1") return json({ ...job, state: "succeeded" })
    return json({ items: [] })
  }))
  render(<MemoryRouter><Watchlist /></MemoryRouter>)
  await screen.findByText("No watched contracts yet")
  fireEvent.click(screen.getByRole("button", { name: "Check expiry results" }))
  expect(await screen.findByText("Expiry results checked.")).toBeTruthy()
})

for (const quota of [false, true]) {
  it(`preserves successful chain watch feedback with storage quota=${quota}`, async () => {
    const page = samplePage()
    page.expirations[0].contracts[0].watch_key = "watchable"
    vi.mocked(fetchChain).mockResolvedValue(page)
    vi.mocked(fetchTickers).mockResolvedValue({ as_of: "2026-10-01T14:00:00Z", total: 0, results: [] })
    vi.mocked(addWatch).mockResolvedValue({ created: true, item, job: quota ? job : null })
    if (quota) vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Storage full", "QuotaExceededError")
    })
    render(<MemoryRouter><ItmChain /></MemoryRouter>)
    const button = await screen.findByRole("button", { name: "Watch IREN 2026-09-18 $50.00 strike" })
    fireEvent.click(button)
    await waitFor(() => expect(button.textContent).toBe("Watching"))
    expect(screen.queryByText("Storage full")).toBeNull()
  })
}
