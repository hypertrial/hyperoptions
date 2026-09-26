// @vitest-environment jsdom

import { act, cleanup, renderHook } from "@testing-library/react"
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
  Object.assign(unsupported.expirations[0].contracts[0], { predictive_odds: { status: "unavailable", reason: "horizon_unsupported" } })
  fetchMock.mockResolvedValue(unsupported)
  renderHook(() => useChainPage("IREN", "call", "itm"))
  await act(async () => { await Promise.resolve() })

  await act(async () => { await vi.advanceTimersByTimeAsync(20 * 60_000) })
  expect(fetchMock).toHaveBeenCalledTimes(1)
})
