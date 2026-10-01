// @vitest-environment jsdom

import { act, cleanup, renderHook } from "@testing-library/react"
import { StrictMode } from "react"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { fetchChain } from "./api"
import { samplePage } from "./testFixtures"
import { useChainPage } from "./useChainPage"

vi.mock("./api", () => ({ fetchChain: vi.fn() }))

const fetchMock = vi.mocked(fetchChain)

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date("2026-09-17T14:00:00Z"))
  Object.defineProperty(document, "hidden", { configurable: true, value: false })
  fetchMock.mockReset()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

it("refreshes a visible chain every five minutes during regular market hours", async () => {
  fetchMock.mockResolvedValue(samplePage())
  renderHook(() => useChainPage("IREN", "call", "itm"))
  await act(async () => { await Promise.resolve() })
  expect(fetchMock).toHaveBeenCalledTimes(1)

  await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000) })
  expect(fetchMock).toHaveBeenCalledTimes(2)
  Object.defineProperty(document, "hidden", { configurable: true, value: true })
  await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000) })
  expect(fetchMock).toHaveBeenCalledTimes(2)
  Object.defineProperty(document, "hidden", { configurable: true, value: false })
  await act(async () => { document.dispatchEvent(new Event("visibilitychange")); await Promise.resolve() })
  expect(fetchMock).toHaveBeenCalledTimes(3)
})

it("clears the previous chain while a new ticker is loading", async () => {
  let releaseNext: (page: ReturnType<typeof samplePage>) => void = () => {}
  fetchMock.mockResolvedValueOnce(samplePage())
  const hook = renderHook(
    ({ ticker }: { ticker: string }) => useChainPage(ticker, "call", "itm"),
    { initialProps: { ticker: "IREN" } },
  )
  await act(async () => { await Promise.resolve() })
  expect(hook.result.current.page?.ticker).toBe("IREN")
  expect(hook.result.current.loading).toBe(false)

  fetchMock.mockImplementationOnce(() => new Promise((resolve) => { releaseNext = resolve }))
  hook.rerender({ ticker: "CIFR" })
  expect(hook.result.current.loading).toBe(true)
  expect(hook.result.current.page).toBeNull()

  await act(async () => {
    releaseNext(samplePage({ ticker: "CIFR" }))
    await Promise.resolve()
  })
  expect(hook.result.current.loading).toBe(false)
  expect(hook.result.current.page?.ticker).toBe("CIFR")
})

it("drops a stale response that arrives after the ticker changes", async () => {
  let releaseFirst: (page: ReturnType<typeof samplePage>) => void = () => {}
  let releaseNext: (page: ReturnType<typeof samplePage>) => void = () => {}
  fetchMock.mockImplementationOnce(() => new Promise((resolve) => { releaseFirst = resolve }))
  const hook = renderHook(
    ({ ticker }: { ticker: string }) => useChainPage(ticker, "call", "itm"),
    { initialProps: { ticker: "IREN" } },
  )
  fetchMock.mockImplementationOnce(() => new Promise((resolve) => { releaseNext = resolve }))
  hook.rerender({ ticker: "CIFR" })

  await act(async () => {
    releaseFirst(samplePage())
    await Promise.resolve()
  })
  expect(hook.result.current.page).toBeNull()
  expect(hook.result.current.loading).toBe(true)

  await act(async () => {
    releaseNext(samplePage({ ticker: "CIFR" }))
    await Promise.resolve()
  })
  expect(hook.result.current.loading).toBe(false)
  expect(hook.result.current.page?.ticker).toBe("CIFR")
})

it("checks pending odds quickly and stops daytime refresh after the close", async () => {
  const pending = samplePage()
  Object.assign(pending.expirations[0].contracts[0], { market_odds: { status: "pending" } })
  fetchMock.mockResolvedValueOnce(pending).mockResolvedValue(samplePage())
  renderHook(() => useChainPage("IREN", "call", "itm"))
  await act(async () => { await Promise.resolve() })
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
  expect(fetchMock).toHaveBeenCalledTimes(2)

  vi.setSystemTime(new Date("2026-09-17T20:00:00Z"))
  await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000) })
  expect(fetchMock).toHaveBeenCalledTimes(2)
})

it("checks a pending predictive forecast after hours and stops when it resolves", async () => {
  vi.setSystemTime(new Date("2026-09-17T21:00:00Z"))
  const pending = samplePage()
  Object.assign(pending.expirations[0].contracts[0], { predictive_odds: { status: "pending" } })
  fetchMock.mockResolvedValueOnce(pending).mockResolvedValue(samplePage())
  renderHook(() => useChainPage("IREN", "call", "itm"))
  await act(async () => { await Promise.resolve() })

  await act(async () => { await vi.advanceTimersByTimeAsync(14_999) })
  expect(fetchMock).toHaveBeenCalledTimes(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(fetchMock).toHaveBeenCalledTimes(2)
  await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000) })
  expect(fetchMock).toHaveBeenCalledTimes(2)
})

it.each(["physical_models", "market_models"])("refreshes a pending %s comparison after hours", async (field) => {
  vi.setSystemTime(new Date("2026-09-17T21:00:00Z"))
  const pending = samplePage()
  Object.assign(pending.expirations[0].contracts[0], { [field]: [{ status: "pending" }] })
  fetchMock.mockResolvedValueOnce(pending).mockResolvedValue(samplePage())
  renderHook(() => useChainPage("IREN", "call", "itm"))
  await act(async () => { await Promise.resolve() })

  await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
  expect(fetchMock).toHaveBeenCalledTimes(2)
  await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000) })
  expect(fetchMock).toHaveBeenCalledTimes(2)
})

it("refreshes missing shared evidence after hours and stops when it arrives", async () => {
  vi.setSystemTime(new Date("2026-09-17T21:00:00Z"))
  const missing = samplePage({ model_evidence: {} })
  Object.assign(missing.expirations[0].contracts[0], {
    physical_models: [{ status: "available", evidence_key: "lognormal_ewma:2-5" }],
  })
  const complete = samplePage({ model_evidence: { "lognormal_ewma:2-5": { prospective: {} } } })
  Object.assign(complete.expirations[0].contracts[0], {
    physical_models: [{ status: "available", evidence_key: "lognormal_ewma:2-5" }],
  })
  fetchMock.mockResolvedValueOnce(missing).mockResolvedValue(complete)
  const hook = renderHook(() => useChainPage("IREN", "call", "itm"))
  await act(async () => { await Promise.resolve() })

  await act(async () => { await vi.advanceTimersByTimeAsync(14_999) })
  expect(fetchMock).toHaveBeenCalledTimes(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(hook.result.current.page?.model_evidence?.["lognormal_ewma:2-5"]).toBeTruthy()
  await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000) })
  expect(fetchMock).toHaveBeenCalledTimes(2)
})

it("stops retrying genuinely absent evidence after eight polls", async () => {
  vi.setSystemTime(new Date("2026-09-17T21:00:00Z"))
  const missing = samplePage({ model_evidence: {} })
  Object.assign(missing.expirations[0].contracts[0], {
    physical_models: [{ status: "available", evidence_key: "lognormal_ewma:2-5" }],
  })
  fetchMock.mockResolvedValue(missing)
  renderHook(() => useChainPage("IREN", "call", "itm"))
  await act(async () => { await Promise.resolve() })

  for (let poll = 0; poll < 8; poll += 1) {
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
  }
  expect(fetchMock).toHaveBeenCalledTimes(9)
  await act(async () => { await vi.advanceTimersByTimeAsync(20 * 60_000) })
  expect(fetchMock).toHaveBeenCalledTimes(9)
})

it.each(["market_data_missing", "market_data_invalid"])("retries temporary %s forecast failure after hours only when visible", async (reason) => {
  vi.setSystemTime(new Date("2026-09-17T21:00:00Z"))
  const unavailable = samplePage()
  Object.assign(unavailable.expirations[0].contracts[0], { predictive_odds: { status: "unavailable", reason } })
  fetchMock.mockResolvedValueOnce(unavailable).mockResolvedValue(samplePage())
  renderHook(() => useChainPage("IREN", "call", "itm"))
  await act(async () => { await Promise.resolve() })

  Object.defineProperty(document, "hidden", { configurable: true, value: true })
  await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000) })
  expect(fetchMock).toHaveBeenCalledTimes(1)
  Object.defineProperty(document, "hidden", { configurable: true, value: false })
  await act(async () => { document.dispatchEvent(new Event("visibilitychange")); await Promise.resolve() })
  expect(fetchMock).toHaveBeenCalledTimes(2)
})

it("does not poll terminal predictive unsupported reasons after hours", async () => {
  vi.setSystemTime(new Date("2026-09-17T21:00:00Z"))
  const unsupported = samplePage()
  Object.assign(unsupported.expirations[0].contracts[0], {
    predictive_odds: { status: "unavailable", reason: "horizon_unsupported" },
    physical_models: [{ status: "unavailable", evidence_key: null }],
  })
  fetchMock.mockResolvedValue(unsupported)
  renderHook(() => useChainPage("IREN", "call", "itm"))
  await act(async () => { await Promise.resolve() })

  await act(async () => { await vi.advanceTimersByTimeAsync(20 * 60_000) })
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it.each([false, true])("shares a slow automatic refresh with manual refresh (failure=%s)", async (fail) => {
  const pending = samplePage()
  pending.expirations[0].contracts[0].market_odds = { status: "pending" }
  let resolveRefresh: (page: ReturnType<typeof samplePage>) => void = () => {}
  let rejectRefresh: (error: Error) => void = () => {}
  fetchMock.mockResolvedValueOnce(pending).mockImplementationOnce(() => new Promise((resolve, reject) => {
    resolveRefresh = resolve
    rejectRefresh = reject
  }))
  const hook = renderHook(() => useChainPage("IREN", "call", "itm"))
  await act(async () => { await Promise.resolve() })
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(hook.result.current.loading).toBe(false)
  await act(async () => { await vi.advanceTimersByTimeAsync(20_000) })
  expect(fetchMock).toHaveBeenCalledTimes(2)
  act(() => { hook.result.current.beginRefresh(); hook.result.current.beginRefresh() })
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(hook.result.current.loading).toBe(true)
  await act(async () => {
    if (fail) rejectRefresh(new Error("The API did not respond."))
    else resolveRefresh(samplePage({ current_cents: 5100 }))
  })
  expect(hook.result.current.loading).toBe(false)
  expect(hook.result.current.page).toEqual(fail ? pending : samplePage({ current_cents: 5100 }))
  expect(hook.result.current.error).toBe(fail ? "The API did not respond." : null)
})

it("aborts replaced and unmounted loads even when their fetch ignores cancellation", async () => {
  const releases: Array<(page: ReturnType<typeof samplePage>) => void> = []
  fetchMock.mockImplementation(() => new Promise((resolve) => { releases.push(resolve) }))
  const hook = renderHook(({ ticker }) => useChainPage(ticker, "call", "itm"), {
    initialProps: { ticker: "IREN" },
  })
  const firstSignal = fetchMock.mock.calls[0][4]
  expect(firstSignal).toBeInstanceOf(AbortSignal)
  hook.rerender({ ticker: "CIFR" })
  const nextSignal = fetchMock.mock.calls[1][4]
  expect(firstSignal!.aborted).toBe(true)
  expect(nextSignal!.aborted).toBe(false)
  await act(async () => { releases[0](samplePage()) })
  expect(hook.result.current.page).toBeNull()
  expect(hook.result.current.loading).toBe(true)
  hook.unmount()
  expect(nextSignal!.aborted).toBe(true)
  await act(async () => { releases[1](samplePage({ ticker: "CIFR" })) })
})

it("starts a fresh load after StrictMode cleanup without accepting the aborted result", async () => {
  const releases: Array<(page: ReturnType<typeof samplePage>) => void> = []
  fetchMock.mockImplementation(() => new Promise((resolve) => { releases.push(resolve) }))
  const hook = renderHook(() => useChainPage("IREN", "call", "itm"), { wrapper: StrictMode })
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(fetchMock.mock.calls[0][4]!.aborted).toBe(true)
  expect(fetchMock.mock.calls[1][4]!.aborted).toBe(false)
  await act(async () => { releases[0](samplePage({ current_cents: 1 })) })
  expect(hook.result.current.page).toBeNull()
  await act(async () => { releases[1](samplePage({ current_cents: 5100 })) })
  expect(hook.result.current.page?.current_cents).toBe(5100)
  expect(hook.result.current.loading).toBe(false)
})

it("counts eight started evidence requests without charging skipped polls and resets on manual refresh", async () => {
  vi.setSystemTime(new Date("2026-09-17T21:00:00Z"))
  const missing = samplePage({ model_evidence: {} })
  missing.expirations[0].contracts[0].physical_models = [{ status: "available", evidence_key: "lognormal_ewma:2-5" }]
  let release: (page: ReturnType<typeof samplePage>) => void = () => {}
  fetchMock.mockResolvedValueOnce(missing).mockImplementation(() => new Promise((resolve) => { release = resolve }))
  const hook = renderHook(() => useChainPage("IREN", "call", "itm"))
  await act(async () => { await Promise.resolve() })
  for (let poll = 0; poll < 8; poll += 1) {
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
    expect(fetchMock).toHaveBeenCalledTimes(poll + 2)
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(fetchMock).toHaveBeenCalledTimes(poll + 2)
    await act(async () => { release(missing) })
  }
  await act(async () => { await vi.advanceTimersByTimeAsync(20 * 60_000) })
  expect(fetchMock).toHaveBeenCalledTimes(9)
  act(() => hook.result.current.beginRefresh())
  expect(fetchMock).toHaveBeenCalledTimes(10)
  await act(async () => { release(missing) })
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
  expect(fetchMock).toHaveBeenCalledTimes(11)
})

it("clears initial loading on a timeout and permits a fresh manual retry", async () => {
  fetchMock.mockRejectedValueOnce(new Error("The API did not respond.")).mockResolvedValueOnce(samplePage())
  const hook = renderHook(() => useChainPage("IREN", "call", "itm"))
  await act(async () => { await Promise.resolve() })
  expect(hook.result.current.loading).toBe(false)
  expect(hook.result.current.page).toBeNull()
  expect(hook.result.current.error).toBe("The API did not respond.")
  await act(async () => { hook.result.current.beginRefresh() })
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(hook.result.current.error).toBeNull()
  expect(hook.result.current.page).toEqual(samplePage())
})

it("clears a synchronously failing request before a manual retry", async () => {
  fetchMock.mockImplementationOnce(() => { throw new Error("Local API unavailable") })
    .mockResolvedValueOnce(samplePage())
  const hook = renderHook(() => useChainPage("IREN", "call", "itm"))
  expect(hook.result.current.error).toBe("Local API unavailable")
  await act(async () => { hook.result.current.beginRefresh() })
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(hook.result.current.page).toEqual(samplePage())
  expect(hook.result.current.loading).toBe(false)
})
