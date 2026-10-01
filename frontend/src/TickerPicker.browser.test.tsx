// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { fetchTickers } from "./api"
import TickerPicker from "./TickerPicker"
import type { TickerSearchResponse } from "./types"

vi.mock("./api", () => ({ fetchTickers: vi.fn() }))

afterEach(() => { cleanup(); vi.useRealTimers(); vi.mocked(fetchTickers).mockReset() })

it("keeps the debounce while aborting replaced and unmounted searches", async () => {
  vi.useFakeTimers()
  const releases: Array<(page: TickerSearchResponse) => void> = []
  vi.mocked(fetchTickers).mockImplementation(() => new Promise((resolve) => { releases.push(resolve) }))
  const onSelect = vi.fn()
  const view = render(<TickerPicker ticker="IREN" onSelect={onSelect} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(150) })
  const initialSignal = vi.mocked(fetchTickers).mock.calls[0][2]!
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "CIFR" } })
  expect(initialSignal.aborted).toBe(true)
  await act(async () => { await vi.advanceTimersByTimeAsync(149) })
  expect(fetchTickers).toHaveBeenCalledTimes(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(fetchTickers).toHaveBeenCalledTimes(2)
  const currentSignal = vi.mocked(fetchTickers).mock.calls[1][2]!
  await act(async () => {
    releases[0]({ as_of: "2026-09-17T14:00:00Z", total: 1, results: [{ symbol: "IREN", name: "Old result" }] })
  })
  expect(screen.queryByRole("option", { name: /Old result/ })).toBeNull()
  expect(screen.queryByText("Ticker list unavailable")).toBeNull()
  await act(async () => {
    releases[1]({ as_of: "2026-09-17T14:00:00Z", total: 1, results: [{ symbol: "CIFR", name: "Cipher Mining" }] })
  })
  fireEvent.click(screen.getByRole("option", { name: /CIFR.*Cipher Mining/ }))
  expect(onSelect).toHaveBeenCalledWith("CIFR")
  view.unmount()
  expect(currentSignal.aborted).toBe(true)
})
