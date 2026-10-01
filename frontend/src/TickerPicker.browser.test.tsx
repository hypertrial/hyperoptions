// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { fetchTickers } from "./api"
import TickerPicker from "./TickerPicker"
import type { TickerSearchResponse } from "./types"

vi.mock("./api", () => ({ fetchTickers: vi.fn() }))

afterEach(() => { cleanup(); vi.useRealTimers(); vi.mocked(fetchTickers).mockReset() })

it("owns popup announcements and preserves editing when suggestions are closed", async () => {
  vi.useFakeTimers()
  vi.mocked(fetchTickers).mockResolvedValue({
    as_of: "2026-09-11T14:00:00Z", total: 2,
    results: [{ symbol: "IREN", name: "Iris Energy" }, { symbol: "CIFR", name: "Cipher Mining" }],
  })
  const onSelect = vi.fn()
  render(<TickerPicker ticker="IREN" onSelect={onSelect} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(150) })
  const input = screen.getByRole("combobox")
  fireEvent.focus(input)
  fireEvent.change(input, { target: { value: "iren" } })
  expect(fetchTickers).toHaveBeenCalledTimes(1)
  expect(screen.queryByText("Searching tickers…")).toBeNull()
  expect(input.getAttribute("aria-expanded")).toBe("true")
  expect(input.getAttribute("aria-activedescendant")).toBe(screen.getByRole("option", { name: /IREN/ }).id)
  fireEvent.keyDown(input, { key: "Escape" })
  expect(input.getAttribute("aria-expanded")).toBe("false")
  expect(input.hasAttribute("aria-activedescendant")).toBe(false)
  expect(fireEvent.keyDown(input, { key: "Home" })).toBe(true)
  expect(fireEvent.keyDown(input, { key: "End" })).toBe(true)
  fireEvent.keyDown(input, { key: "Enter" })
  expect(onSelect).not.toHaveBeenCalled()
  fireEvent.keyDown(input, { key: "ArrowDown" })
  expect(input.getAttribute("aria-activedescendant")).toBe(screen.getByRole("option", { name: /IREN/ }).id)
  fireEvent.keyDown(input, { key: "ArrowDown" })
  expect(input.getAttribute("aria-activedescendant")).toBe(screen.getByRole("option", { name: /CIFR/ }).id)
  fireEvent.keyDown(input, { key: "Enter" })
  expect(onSelect).toHaveBeenCalledWith("CIFR")
  expect(input.getAttribute("aria-expanded")).toBe("false")
  fireEvent.keyDown(input, { key: "ArrowUp" })
  expect(input.getAttribute("aria-activedescendant")).toBe(screen.getByRole("option", { name: /CIFR/ }).id)
})

it("keeps the debounce while aborting replaced and unmounted searches", async () => {
  vi.useFakeTimers()
  const releases: Array<(page: TickerSearchResponse) => void> = []
  vi.mocked(fetchTickers).mockImplementation(() => new Promise((resolve) => { releases.push(resolve) }))
  const onSelect = vi.fn()
  const view = render(<TickerPicker ticker="IREN" onSelect={onSelect} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(150) })
  const initialSignal = vi.mocked(fetchTickers).mock.calls[0][2]!
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "iren" } })
  expect(initialSignal.aborted).toBe(false)
  expect(fetchTickers).toHaveBeenCalledTimes(1)
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

it("restarts a matching query when the parent ticker selection catches up", async () => {
  vi.useFakeTimers()
  vi.mocked(fetchTickers).mockResolvedValue({
    as_of: "2026-09-11T14:00:00Z", total: 1,
    results: [{ symbol: "CIFR", name: "Cipher Mining" }],
  })
  const view = render(<TickerPicker ticker="IREN" onSelect={vi.fn()} />)
  const input = screen.getByRole("combobox")
  fireEvent.change(input, { target: { value: "CIFR" } })
  await act(async () => { await vi.advanceTimersByTimeAsync(150) })
  view.rerender(<TickerPicker ticker="CIFR" onSelect={vi.fn()} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(150) })
  expect(screen.getByRole("option", { name: /CIFR/ })).toBeTruthy()
  expect(screen.queryByText("Searching tickers…")).toBeNull()
})
