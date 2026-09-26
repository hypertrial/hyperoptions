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
