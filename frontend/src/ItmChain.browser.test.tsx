// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { fetchChain, fetchTickers } from "./api"
import { COPY_HEADERS, formatContractValues, formatRowClipboard } from "./copyRow"
import ItmChain from "./ItmChain"
import { largeChainPage, samplePage, samplePutPage } from "./testFixtures"
import type { CoveredCallPage } from "./types"

vi.mock("./api", () => ({
  fetchChain: vi.fn(),
  fetchTickers: vi.fn(),
}))

const fetchMock = vi.mocked(fetchChain)
const tickersMock = vi.mocked(fetchTickers)
const writeText = vi.fn().mockResolvedValue(undefined)

function setDesktopViewport(desktop: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query.includes("min-width") ? desktop : !desktop,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
}

const LISTINGS = [
  { symbol: "CIFR", name: "Cipher Mining Inc.", sector: "Finance", industry: "Crypto" },
  { symbol: "IREN", name: "Iris Energy Limited", sector: "Finance", industry: "Crypto" },
  { symbol: "NBIS", name: "Nebius Group N.V.", sector: "Technology", industry: "Software" },
  { symbol: "WULF", name: "TeraWulf Inc.", sector: "Finance", industry: "Crypto" },
]

function page(overrides: Partial<CoveredCallPage> = {}): CoveredCallPage {
  return samplePage(overrides)
}

async function selectTicker(symbol: string) {
  const input = screen.getByRole("combobox")
  fireEvent.focus(input)
  fireEvent.change(input, { target: { value: symbol } })
  const option = await screen.findByRole("option", { name: new RegExp(symbol) })
  fireEvent.click(option)
}

describe("chain interactions", () => {
  beforeEach(() => {
    setDesktopViewport(true)
    window.history.replaceState(null, "", "/")
    fetchMock.mockReset()
    fetchMock.mockResolvedValue(page())
    tickersMock.mockReset()
    tickersMock.mockImplementation(async (query: string) => {
      const needle = query.trim().toUpperCase()
      const results = LISTINGS.filter((item) => (
        item.symbol.startsWith(needle) || item.name.toUpperCase().includes(needle)
      ))
      return { as_of: "2026-09-11T14:00:00Z", total: LISTINGS.length, results }
    })
    writeText.mockReset()
    writeText.mockResolvedValue(undefined)
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } })
  })
  afterEach(cleanup)

  it("loads IREN first, groups expirations, and sorts strikes high to low", async () => {
    render(<ItmChain />)

    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy())
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith("IREN", "call", "itm")
    const first = screen.getByRole("heading", { name: /2026-09-18/ }).closest("section")
    const second = screen.getByRole("heading", { name: /2026-10-09/ }).closest("section")
    expect(first).not.toBeNull()
    expect(second).not.toBeNull()
    expect(first!.compareDocumentPosition(second!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(first!.querySelector(".expiry-trigger")?.getAttribute("aria-expanded")).toBe("true")
    expect(second!.querySelector(".expiry-trigger")?.getAttribute("aria-expanded")).toBe("false")
    expect(screen.getByLabelText("2 contracts").textContent).toBe("2 contracts")
    expect(screen.getByLabelText("1 contract").textContent).toBe("1 contract")
    expect(within(second!).queryByRole("table")).toBeNull()
    expect(screen.queryByRole("tablist")).toBeNull()
    expect(screen.getByRole("combobox")).toBeTruthy()
    const strikes = within(first!).getAllByRole("row").slice(1).map((row) => row.firstChild?.textContent)
    expect(strikes).toEqual(["$50.00", "$40.50"])
    expect(within(first!).getAllByRole("row")[1].querySelector("th")?.hasAttribute("data-heat")).toBe(false)
    expect(within(first!).getAllByRole("row")[1].querySelector("[data-heat]")?.textContent).toBe("$40.00")
    expect(within(first!).getAllByRole("row")[2].querySelector("th")?.hasAttribute("data-heat")).toBe(false)
    expect(within(first!).getAllByRole("row")[2].querySelector("td")?.hasAttribute("data-heat")).toBe(false)
    const pricedCells = within(first!).getAllByRole("row")[1].querySelectorAll("td")
    expect(pricedCells[0].textContent).toBe("$0.50")
    expect(pricedCells[0].hasAttribute("data-heat")).toBe(false)
    expect(pricedCells[4].textContent).toBe("$40.00")
    expect(pricedCells[4].getAttribute("data-heat")).toBe("0.50")
    const missingRowCells = within(first!).getAllByRole("row")[2].querySelectorAll("td")
    expect(missingRowCells[4].textContent).toBe("—")
    expect(missingRowCells[4].hasAttribute("data-heat")).toBe(false)
    const missingCells = [...missingRowCells].map((cell) => cell.textContent)
    expect(missingCells.slice(0, 7)).toEqual(["—", "—", "—", "—", "—", "—", "—"])
    expect(screen.queryByText("Suggested trade")).toBeNull()
    expect(screen.getByRole("button", { name: "Copy row IREN 2026-09-18 strike $50.00" })).toBeTruthy()
    expect(screen.getByText("Market open")).toBeTruthy()
    expect(screen.getByText(/Stock bid/)).toBeTruthy()
    expect(screen.getByText(/\$49\.90/)).toBeTruthy()
    expect(screen.getByText(/Sep 11, 2026 10:00 AM ET/)).toBeTruthy()
    expect(screen.queryByText(/SEP 10, 2026 3:37 PM ET/)).toBeNull()
    expect(screen.getByText("1 contract · 100 sh · $4,990.00 stock")).toBeTruthy()
    expect(screen.getByText("1.0%")).toBeTruthy()
    const headers = within(first!).getAllByRole("columnheader")
    expect(headers.map((header) => header.textContent)).toEqual([
      "Strike",
      "Bid",
      "Sprd %",
      "OI",
      "Premium",
      "Called P&L",
      "APR (net)",
      "Drop (BE)",
      "Copy",
    ])
    expect(headers.some((header) => header.textContent === "IV")).toBe(false)
    expect(headers[3].querySelector("abbr")?.getAttribute("title")).toBe(headers[3].getAttribute("title"))
    expect(headers[3].getAttribute("title")).toContain("open interest")
    expect(headers[5].getAttribute("title")).toContain("assigned")
    expect(headers[1].getAttribute("title")).toContain("Premium is 100")
    expect(screen.getByRole("main").getAttribute("aria-busy")).toBe("false")
    expect(screen.getByRole("button", { name: "Filters" }).getAttribute("aria-expanded")).toBe("false")
    expect(screen.getByLabelText("Min Called P&L ($)")).toBeTruthy()
    expect(screen.getByLabelText("Min APR net (%)")).toBeTruthy()
    expect(screen.getByLabelText("Min Drop to breakeven (%)")).toBeTruthy()
    expect(screen.getByLabelText("Min DTE")).toBeTruthy()
    expect(screen.getByLabelText("Max DTE")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "Clear all" })).toBeNull()
    expect(screen.getByRole("radio", { name: "Covered calls" })).toBeTruthy()
    expect(screen.getByRole("radio", { name: "Cash-secured puts" })).toBeTruthy()
    expect(screen.getByRole("radio", { name: "ITM" })).toBeTruthy()
    expect(screen.getByRole("radio", { name: "OTM" })).toBeTruthy()
    expect(screen.getByRole("radio", { name: "All" })).toBeTruthy()
  })

  it("opens the custom ticker listbox on type without a trigger chevron", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const input = screen.getByRole("combobox")
    expect(input.closest("label")?.querySelector("button")).toBeNull()
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: "WULF" } })
    const option = await screen.findByRole("option", { name: /WULF/ })
    const listbox = screen.getByRole("listbox", { name: "Matching tickers" })
    expect(input.getAttribute("aria-controls")).toBe(listbox.getAttribute("id"))
    expect(listbox.contains(option)).toBe(true)
    expect(screen.queryByRole("tablist")).toBeNull()
  })

  it("shows truthful ticker-search progress before reporting no matches", async () => {
    let release: (() => void) | undefined
    tickersMock.mockImplementation(async () => {
      await new Promise<void>((resolve) => {
        release = resolve
      })
      return { as_of: "2026-09-11T14:00:00Z", total: LISTINGS.length, results: [] }
    })
    render(<ItmChain />)
    const input = screen.getByRole("combobox")
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: "ZZ" } })

    const listbox = screen.getByRole("listbox", { name: "Matching tickers" })
    expect(listbox.getAttribute("aria-busy")).toBe("true")
    expect(screen.getByRole("option", { name: "Searching tickers…" })).toBeTruthy()
    expect(screen.queryByRole("option", { name: "No matches" })).toBeNull()

    await waitFor(() => expect(tickersMock).toHaveBeenCalledWith("ZZ"))
    release?.()
    await waitFor(() => expect(screen.getByRole("option", { name: "No matches" })).toBeTruthy())
    expect(listbox.getAttribute("aria-busy")).toBe("false")
  })

  it("sizes buy-writes from a contract count and scales outlay and called P&L", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$40.00")).toBeTruthy())
    fireEvent.change(screen.getByLabelText("Contracts"), { target: { value: "2" } })
    expect(screen.getByText("2 contracts · 200 sh · $9,980.00 stock")).toBeTruthy()
    expect(screen.getByText("$100.00")).toBeTruthy()
    expect(screen.getByText("$80.00")).toBeTruthy()
    expect(screen.getByText("42.1%")).toBeTruthy()
  })

  it("treats an explicit zero contract count as invalid and keeps one contract", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$40.00")).toBeTruthy())
    fireEvent.change(screen.getByLabelText("Contracts"), { target: { value: "0" } })
    expect(screen.getByLabelText("Contracts").getAttribute("aria-invalid")).toBe("true")
    expect(screen.getByText(/Enter a whole number of 1 or more/)).toBeTruthy()
    expect(screen.getByText("$40.00")).toBeTruthy()
  })

  it("rejects a contract count that would overflow scaled money", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$40.00")).toBeTruthy())
    fireEvent.change(screen.getByLabelText("Contracts"), {
      target: { value: String(Number.MAX_SAFE_INTEGER) },
    })
    expect(screen.getByLabelText("Contracts").getAttribute("aria-invalid")).toBe("true")
    expect(screen.getByText(/too large to calculate exactly/)).toBeTruthy()
    expect(screen.getByText("$40.00")).toBeTruthy()
  })

  it("fetches another ticker only after it is chosen from the picker", async () => {
    let release: (() => void) | undefined
    fetchMock.mockImplementation(async (selected) => {
      if (selected === "CIFR") {
        await new Promise<void>((resolve) => {
          release = resolve
        })
      }
      return page({ ticker: selected })
    })
    render(<ItmChain />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))

    await selectTicker("CIFR")
    await waitFor(() => expect(screen.getByText("Loading CIFR market data")).toBeTruthy())
    expect(screen.getByText("Loading CIFR quote")).toBeTruthy()
    expect(screen.getByText("Fetching the latest quote and option chain. This can take a few seconds.")).toBeTruthy()
    expect(screen.queryByText("Session unavailable")).toBeNull()
    expect(screen.queryByText("No usable price")).toBeNull()
    expect(screen.queryByText(/0 contracts/)).toBeNull()
    expect(screen.queryByText("$49.90")).toBeNull()
    expect(screen.queryByRole("button", { name: "Filters" })).toBeNull()
    expect(screen.getByRole("main").getAttribute("aria-busy")).toBe("true")
    expect(screen.getByRole("region", { name: "CIFR market summary" }).getAttribute("aria-busy")).toBe("true")
    release?.()
    await waitFor(() => expect(screen.queryByText("Loading CIFR market data")).toBeNull())
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("CIFR", "call", "itm"))
    expect(fetchMock.mock.calls.map((item) => item[0])).toEqual(["IREN", "CIFR"])
  })

  it("shows the suspended ticker-change state in the mobile summary and drawer", async () => {
    setDesktopViewport(false)
    let release: (() => void) | undefined
    fetchMock.mockImplementation(async (selected) => {
      if (selected === "CIFR") {
        await new Promise<void>((resolve) => {
          release = resolve
        })
      }
      return page({ ticker: selected })
    })
    render(<ItmChain />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByRole("button", { name: "Settings" }))
    await screen.findByText("Market setup")
    await selectTicker("CIFR")

    await screen.findByText("Loading CIFR market data")
    const mobileLoading = screen.getByText("Loading market data…")
    expect(mobileLoading.closest(".mobile-market-quote")?.getAttribute("aria-busy")).toBe("true")
    expect(screen.getByText("Loading CIFR quote")).toBeTruthy()
    expect(screen.getByRole("region", { name: "CIFR market summary" }).getAttribute("aria-busy")).toBe("true")
    expect(screen.getByRole("main").getAttribute("aria-busy")).toBe("true")
    expect(screen.queryByText("Session unavailable")).toBeNull()
    expect(screen.queryByText("No usable price")).toBeNull()
    expect(screen.queryByText("$49.90")).toBeNull()
    expect(screen.queryByRole("button", { name: "Filters" })).toBeNull()

    release?.()
    await waitFor(() => expect(screen.queryByText("Loading CIFR market data")).toBeNull())
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("CIFR", "call", "itm"))
  })

  it("ignores a late IREN response after switching to CIFR", async () => {
    let releaseIren: (() => void) | undefined
    fetchMock.mockImplementation(async (selected) => {
      if (selected === "IREN") {
        await new Promise<void>((resolve) => {
          releaseIren = resolve
        })
        return page({ ticker: "IREN" })
      }
      return page({
        ticker: "CIFR",
        expirations: [
          {
            expiration: "2026-11-20",
            dte: 70,
            contracts: [page().expirations[1].contracts[0]],
          },
        ],
      })
    })
    render(<ItmChain />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("IREN", "call", "itm"))
    await selectTicker("CIFR")
    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-11-20/ })).toBeTruthy())
    releaseIren?.()
    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-11-20/ })).toBeTruthy())
    expect(screen.queryByRole("heading", { name: /2026-09-18/ })).toBeNull()
    expect(screen.queryByRole("heading", { name: /2026-10-09/ })).toBeNull()
  })

  it("shows the last-trade timestamp when it supplies the current price", async () => {
    fetchMock.mockResolvedValue(page({
      current_source: "chain_last_trade",
      current_cents: 4393,
    }))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText(/Chain last trade/)).toBeTruthy())
    expect(screen.getByText(/SEP 10, 2026 3:37 PM ET/)).toBeTruthy()
    expect(screen.queryByText(/Sep 11, 2026 10:00 AM ET/)).toBeNull()
  })

  it.each([
    ["stock_bid", "SEP 10, 2026 3:37 PM ET", { quote_timestamp: null }],
    ["chain_last_trade", "Sep 11, 2026 10:00 AM ET", { last_trade_timestamp: null }],
  ] as const)("does not borrow the other source's timestamp for %s", async (currentSource, unrelatedStamp, timestamps) => {
    fetchMock.mockResolvedValue(page({ current_source: currentSource, ...timestamps }))
    render(<ItmChain />)

    await waitFor(() => expect(screen.getByText(currentSource === "stock_bid" ? /Stock bid/ : /Chain last trade/)).toBeTruthy())
    expect(screen.queryByText(new RegExp(unrelatedStamp))).toBeNull()
  })

  it("refreshes the selected ticker and surfaces provider errors and truncation", async () => {
    fetchMock
      .mockResolvedValueOnce(page({ truncated: true }))
      .mockRejectedValueOnce(new Error("Nasdaq unavailable"))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText(/truncated chain/)).toBeTruthy())

    fireEvent.click(screen.getByRole("button", { name: "Refresh data" }))
    await waitFor(() => expect(screen.getByText(/Nasdaq unavailable/)).toBeTruthy())
    expect(screen.getByText("$40.00")).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls.map((item) => item[0])).toEqual(["IREN", "IREN"])
  })

  it("focuses an initial provider failure on recovery instead of zero-result controls", async () => {
    let release: (() => void) | undefined
    fetchMock
      .mockRejectedValueOnce(new Error("Nasdaq unavailable"))
      .mockImplementationOnce(async () => {
        await new Promise<void>((resolve) => {
          release = resolve
        })
        return page()
      })
    render(<ItmChain />)

    const alert = await screen.findByRole("alert")
    expect(alert.textContent).toContain("Couldn’t load IREN market data")
    expect(alert.textContent).toContain("Nasdaq unavailable")
    expect(alert.textContent).toContain("Check the ticker selection or try the request again.")
    expect(screen.getByText(/IREN.*ITM.*Data unavailable/)).toBeTruthy()
    expect(screen.queryByText(/0 contracts/)).toBeNull()
    expect(screen.queryByRole("button", { name: "Filters" })).toBeNull()

    fireEvent.click(screen.getByRole("button", { name: "Try again" }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    expect(screen.queryByRole("alert")).toBeNull()
    expect(screen.getByText("Loading IREN market data")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "Filters" })).toBeNull()

    release?.()
    await waitFor(() => expect(screen.getByRole("button", { name: "Filters" })).toBeTruthy())
    expect(screen.queryByText("Loading IREN market data")).toBeNull()
  })

  it("keeps an empty-chain explanation when a retained refresh fails", async () => {
    fetchMock
      .mockResolvedValueOnce(page({ expirations: [] }))
      .mockRejectedValueOnce(new Error("Nasdaq unavailable"))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("No ITM calls for IREN")).toBeTruthy())

    fireEvent.click(screen.getByRole("button", { name: "Refresh data" }))
    await waitFor(() => expect(screen.getByText(/Refresh failed/)).toBeTruthy())
    expect(screen.getByText("No ITM calls for IREN")).toBeTruthy()
  })

  it("keeps a filter-miss explanation when a retained refresh fails", async () => {
    fetchMock
      .mockResolvedValueOnce(page())
      .mockRejectedValueOnce(new Error("Nasdaq unavailable"))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getAllByText("$50.00")[0]).toBeTruthy())
    fireEvent.change(screen.getByLabelText("Min APR net (%)"), { target: { value: "500" } })
    expect(screen.getByText("No rows match the current filters.")).toBeTruthy()

    fireEvent.click(screen.getByRole("button", { name: "Refresh data" }))
    await waitFor(() => expect(screen.getByText(/Refresh failed/)).toBeTruthy())
    expect(screen.getByText("No rows match the current filters.")).toBeTruthy()
  })

  it("does not refetch when the same ticker is chosen again", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    await selectTicker("IREN")
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("shows an unpriced empty state without inventing metrics", async () => {
    fetchMock.mockResolvedValue(page({ current_cents: null, current_source: null, expirations: [] }))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText(/No usable IREN price is available/)).toBeTruthy())
    expect(screen.queryByRole("table")).toBeNull()
  })

  it("shows an empty ITM state when a usable price has no qualifying calls", async () => {
    fetchMock.mockResolvedValue(page({ expirations: [] }))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText(/No ITM calls for IREN/)).toBeTruthy())
    expect(screen.queryByRole("table")).toBeNull()
  })

  it("shows a non-optionable empty state", async () => {
    fetchMock.mockResolvedValue(page({ options_available: false, expirations: [] }))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("Options are not available for IREN")).toBeTruthy())
    expect(screen.queryByRole("table")).toBeNull()
  })

  it("disables ticker submit when the universe is unavailable", async () => {
    tickersMock.mockRejectedValue(new Error("Ticker universe unavailable"))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("Ticker list unavailable")).toBeTruthy())
    expect(screen.getByRole("combobox")).toHaveProperty("disabled", true)
  })

  it("hides rows below each minimum and ANDs active filters", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$40.00")).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))

    fireEvent.change(screen.getByLabelText("Min Called P&L ($)"), { target: { value: "100" } })
    expect(screen.getByText("$290.00")).toBeTruthy()
    expect(screen.queryByText("$40.00")).toBeNull()
    expect(screen.queryByRole("heading", { name: /2026-09-18/ })).toBeNull()
    expect(screen.getByRole("heading", { name: /2026-10-09/ })).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(1)

    fireEvent.change(screen.getByLabelText("Min Called P&L ($)"), { target: { value: "" } })
    fireEvent.change(screen.getByLabelText("Min APR net (%)"), { target: { value: "50" } })
    expect(screen.getByText("89.8%")).toBeTruthy()
    expect(screen.queryByText("42.1%")).toBeNull()
    expect(screen.queryByRole("heading", { name: /2026-09-18/ })).toBeNull()

    fireEvent.change(screen.getByLabelText("Min APR net (%)"), { target: { value: "" } })
    fireEvent.change(screen.getByLabelText("Min Drop to breakeven (%)"), { target: { value: "5" } })
    expect(screen.getByText("16.0%")).toBeTruthy()
    expect(screen.queryByText("$50.00")).toBeNull()

    fireEvent.change(screen.getByLabelText("Min APR net (%)"), { target: { value: "50" } })
    expect(screen.getByText("$45.00")).toBeTruthy()
    expect(screen.queryByText("$40.50")).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("treats invalid text as inactive and zero as an active floor", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getAllByText("$50.00")[0]).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))

    fireEvent.change(screen.getByLabelText("Min APR net (%)"), { target: { value: "abc" } })
    expect(screen.getAllByText("$50.00")[0]).toBeTruthy()
    expect(screen.getByText("$40.50")).toBeTruthy()
    expect(screen.getByLabelText("Min APR net (%)").getAttribute("aria-invalid")).toBe("true")
    expect(screen.getByRole("button", { name: "Clear all" })).toBeTruthy()

    fireEvent.change(screen.getByLabelText("Min Called P&L ($)"), { target: { value: "0" } })
    expect(screen.getAllByText("$50.00")[0]).toBeTruthy()
    expect(screen.queryByText("$40.50")).toBeNull()
  })

  it("shows removable active-filter chips and clears filters independently", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$40.00")).toBeTruthy())
    fireEvent.change(screen.getByLabelText("Min APR net (%)"), { target: { value: "42.1" } })
    fireEvent.change(screen.getByLabelText("Min DTE"), { target: { value: "7" } })

    expect(screen.getByRole("button", { name: "Remove filter APR net (%) 42.1" })).toBeTruthy()
    expect(screen.getByRole("button", { name: "Remove filter DTE ≥ 7" })).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "Remove filter APR net (%) 42.1" }))
    expect((screen.getByLabelText("Min APR net (%)") as HTMLInputElement).value).toBe("")
    expect((screen.getByLabelText("Min DTE") as HTMLInputElement).value).toBe("7")
    expect(screen.getByRole("button", { name: "Clear all" })).toBeTruthy()
  })

  it("filters contract-scaled Called P&L and keeps heatmap on remaining rows", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$40.00")).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))

    fireEvent.change(screen.getByLabelText("Min Called P&L ($)"), { target: { value: "50" } })
    expect(screen.queryByText("$40.00")).toBeNull()
    expect(screen.getByText("$290.00")).toBeTruthy()

    fireEvent.change(screen.getByLabelText("Contracts"), { target: { value: "2" } })
    expect(screen.getByText("$80.00")).toBeTruthy()
    expect(screen.getByText("$580.00")).toBeTruthy()
    expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy()

    fireEvent.change(screen.getByLabelText("Min Called P&L ($)"), { target: { value: "100" } })
    expect(screen.queryByText("$80.00")).toBeNull()
    expect(screen.getByText("$580.00")).toBeTruthy()
    expect(screen.queryByRole("heading", { name: /2026-09-18/ })).toBeNull()
    const remaining = screen.getByRole("heading", { name: /2026-10-09/ }).closest("section")
    expect(within(remaining!).getAllByRole("row")[1].querySelector("[data-heat]")?.getAttribute("data-heat")).toBe("0.50")
  })

  it("keeps filter text across ticker, refresh, and contracts, and Clear restores the chain", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))
    fireEvent.change(screen.getByLabelText("Min APR net (%)"), { target: { value: "50" } })
    fireEvent.change(screen.getByLabelText("Contracts"), { target: { value: "2" } })
    expect(screen.getByText("89.8%")).toBeTruthy()
    expect(screen.queryByText("42.1%")).toBeNull()

    await selectTicker("CIFR")
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("CIFR", "call", "itm"))
    expect((screen.getByLabelText("Min APR net (%)") as HTMLInputElement).value).toBe("50")
    expect((screen.getByLabelText("Contracts") as HTMLInputElement).value).toBe("2")
    expect(screen.queryByText("42.1%")).toBeNull()
    expect(screen.getByText("89.8%")).toBeTruthy()

    fireEvent.click(screen.getByRole("button", { name: "Refresh data" }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
    expect((screen.getByLabelText("Min APR net (%)") as HTMLInputElement).value).toBe("50")

    fireEvent.click(screen.getByRole("button", { name: "Clear all" }))
    expect((screen.getByLabelText("Min APR net (%)") as HTMLInputElement).value).toBe("")
    expect((screen.getByLabelText("Contracts") as HTMLInputElement).value).toBe("2")
    const restoredExpiry = screen.getByRole("heading", { name: /2026-09-18/ }).closest("section")
    expect(restoredExpiry).toBeTruthy()
    expect(restoredExpiry!.querySelector(".expiry-trigger")?.getAttribute("aria-expanded")).toBe("false")
    expect(screen.queryByText("42.1%")).toBeNull()
    expect(screen.getByText("89.8%")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "Clear all" })).toBeNull()
  })

  it("shows a filter-empty state distinct from no provider contracts", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getAllByText("$50.00")[0]).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))
    fireEvent.change(screen.getByLabelText("Min APR net (%)"), { target: { value: "500" } })
    expect(screen.getByText("No rows match the current filters.")).toBeTruthy()
    expect(screen.queryByText("No ITM calls for IREN")).toBeNull()
    expect(screen.queryByRole("table")).toBeNull()
  })

  it("keeps a row whose displayed Called P&L equals the typed minimum", async () => {
    const sample = page()
    fetchMock.mockResolvedValue(page({
      expirations: [{
        expiration: "2026-09-18",
        dte: 7,
        contracts: [{ ...sample.expirations[0].contracts[0], called_pnl_cents: 4000 }],
      }],
    }))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$40.00")).toBeTruthy())

    fireEvent.change(screen.getByLabelText("Min Called P&L ($)"), { target: { value: "40" } })
    expect(screen.getByText("$40.00")).toBeTruthy()
    expect(screen.getByRole("button", { name: "Copy row IREN 2026-09-18 strike $50.00" })).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("hides expiries outside an inclusive DTE range", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))

    fireEvent.change(screen.getByLabelText("Min DTE"), { target: { value: "21" } })
    expect(screen.queryByRole("heading", { name: /2026-09-18/ })).toBeNull()
    expect(screen.getByRole("heading", { name: /2026-10-09/ })).toBeTruthy()

    fireEvent.change(screen.getByLabelText("Min DTE"), { target: { value: "" } })
    fireEvent.change(screen.getByLabelText("Max DTE"), { target: { value: "14" } })
    expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy()
    expect(screen.queryByRole("heading", { name: /2026-10-09/ })).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("treats an inverted DTE range as a filter miss and marks both fields invalid", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getAllByText("$50.00")[0]).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))
    fireEvent.change(screen.getByLabelText("Min DTE"), { target: { value: "40" } })
    fireEvent.change(screen.getByLabelText("Max DTE"), { target: { value: "10" } })
    expect(screen.getByText("No rows match the current filters.")).toBeTruthy()
    expect(screen.getByLabelText("Min DTE").getAttribute("aria-invalid")).toBe("true")
    expect(screen.getByLabelText("Max DTE").getAttribute("aria-invalid")).toBe("true")
  })

  it("copies displayed row values with headers and context, including contract scaling", async () => {
    const sample = page()
    render(<ItmChain />)
    await waitFor(() => expect(screen.getAllByText("$50.00")[0]).toBeTruthy())

    fireEvent.click(screen.getByRole("button", { name: "Copy row IREN 2026-09-18 strike $50.00" }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    expect(writeText.mock.calls[0][0]).toBe(formatRowClipboard(
      {
        ticker: "IREN",
        expiration: "2026-09-18",
        dte: 7,
        currentSource: "Stock bid",
        currentCents: 4990,
        contracts: 1,
      },
      COPY_HEADERS,
      formatContractValues(sample.expirations[0].contracts[0]),
    ))
    expect(writeText.mock.calls[0][0]).toContain("1 contract · 100 sh")
    expect(writeText.mock.calls[0][0]).not.toMatch(/\| Copy \|/)
    expect(screen.getByText("Copied")).toBeTruthy()

    fireEvent.change(screen.getByLabelText("Contracts"), { target: { value: "2" } })
    fireEvent.click(screen.getByRole("button", { name: "Copy row IREN 2026-09-18 strike $50.00" }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2))
    const sized = writeText.mock.calls[1][0] as string
    expect(sized).toContain("IREN · 2026-09-18 · 7 DTE · Stock bid: $49.90 · 2 contracts · 200 sh")
    expect(sized).toContain("| $50.00 | $0.50 | 2.0% | 55 | $100.00 | $80.00 | 42.1% | 1.0% |")

    fireEvent.change(screen.getByLabelText("Contracts"), { target: { value: "0" } })
    fireEvent.click(screen.getByRole("button", { name: "Copy row IREN 2026-09-18 strike $50.00" }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(3))
    expect(writeText.mock.calls[2][0]).toContain("1 contract · 100 sh")
  })

  it("does not claim Copied when clipboard write fails", async () => {
    writeText.mockRejectedValueOnce(new Error("denied"))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getAllByText("$50.00")[0]).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Copy row IREN 2026-09-18 strike $50.00" }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    expect(screen.queryByText("Copied")).toBeNull()
  })

  it("does not keep Copied on another ticker with the same expiration and strike", async () => {
    const iren = page()
    const sameStrike = iren.expirations[0].contracts[0]
    fetchMock.mockImplementation(async (selected) => {
      if (selected === "CIFR") {
        return page({
          ticker: "CIFR",
          expirations: [{
            expiration: sameStrike.expiration,
            dte: 7,
            contracts: [{ ...sameStrike }],
          }],
        })
      }
      return iren
    })
    render(<ItmChain />)
    await waitFor(() => expect(screen.getAllByText("$50.00")[0]).toBeTruthy())

    fireEvent.click(screen.getByRole("button", { name: "Copy row IREN 2026-09-18 strike $50.00" }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    expect(screen.getByText("Copied")).toBeTruthy()

    await selectTicker("CIFR")
    await waitFor(() => expect(screen.getByRole("button", { name: "Copy row CIFR 2026-09-18 strike $50.00" })).toBeTruthy())
    expect(screen.getByRole("button", { name: "Copy row CIFR 2026-09-18 strike $50.00" }).getAttribute("data-copied")).toBeNull()
    expect(screen.queryByText("Copied")).toBeNull()
  })

  it("bounds the initial DOM for a large chain and reveals more on request", async () => {
    fetchMock.mockResolvedValue(largeChainPage(400))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText(/Displaying 250 of 400 rows/)).toBeTruthy())
    expect(document.querySelectorAll("tbody tr")).toHaveLength(250)
    fireEvent.click(screen.getByRole("button", { name: "Show 250 more" }))
    expect(screen.queryByText(/Displaying/)).toBeNull()
    expect(document.querySelectorAll("tbody tr")).toHaveLength(400)
  })

  it("resets the reveal window when filters change", async () => {
    fetchMock.mockResolvedValue(largeChainPage(300))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText(/Displaying 250 of 300 rows/)).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Show all" }))
    expect(document.querySelectorAll("tbody tr")).toHaveLength(300)
    fireEvent.change(screen.getByLabelText("Min DTE"), { target: { value: "1" } })
    expect(screen.getByText(/Displaying 250 of 300 rows/)).toBeTruthy()
    expect(document.querySelectorAll("tbody tr")).toHaveLength(250)
  })

  it("refetches puts with OTM default and put filters", async () => {
    fetchMock.mockImplementation(async (_ticker, side) => (
      side === "put" ? samplePutPage() : page()
    ))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$40.00")).toBeTruthy())

    fireEvent.click(screen.getByRole("radio", { name: "Cash-secured puts" }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("IREN", "put", "otm"))
    expect(screen.getByRole("heading", { name: "Cash-secured puts" })).toBeTruthy()
    expect(screen.getByText("Iris Energy Limited")).toBeTruthy()
    expect(screen.getByLabelText("Min Premium ($)")).toBeTruthy()
    expect(screen.getByLabelText("Min APR net (%)")).toBeTruthy()
    expect(screen.getByLabelText("Min Cushion to breakeven (%)")).toBeTruthy()
    expect(screen.getByText("$45.00")).toBeTruthy()
    expect(screen.getByText("$44.20")).toBeTruthy()
    expect(screen.queryByText("Called P&L")).toBeNull()
  })

  it("honors explicit URL columns on load and resets them for a deliberate strategy switch", async () => {
    window.history.replaceState(null, "", "/?t=IREN&side=call&m=itm&cols=strike_cents,iv_pct_tenths")
    fetchMock.mockImplementation(async (_ticker, selectedSide) => (
      selectedSide === "put" ? samplePutPage() : page()
    ))
    render(<ItmChain />)

    await waitFor(() => expect(screen.getByRole("columnheader", { name: "IV" })).toBeTruthy())
    expect(screen.getAllByRole("columnheader").map((header) => header.textContent)).toEqual(["Strike", "IV", "Copy"])
    fireEvent.click(screen.getByRole("radio", { name: "Cash-secured puts" }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("IREN", "put", "otm"))
    expect(screen.queryByRole("columnheader", { name: "IV" })).toBeNull()
    expect(screen.getAllByRole("columnheader").map((header) => header.textContent)).toEqual([
      "Strike", "Bid", "Sprd %", "OI", "Premium", "Breakeven", "APR (net)", "Cushion (BE)", "Copy",
    ])
    expect(window.location.search).not.toContain("cols=")
  })

  it("keeps the column picker synchronized with canonical URL columns", async () => {
    window.history.replaceState(null, "", "/?t=IREN&side=put&m=otm&cols=call_bid_cents")
    fetchMock.mockResolvedValue(samplePutPage())
    render(<ItmChain />)

    await screen.findByRole("columnheader", { name: "Strike" })
    expect(screen.getByRole("button", { name: "Columns8" })).toBeTruthy()
    expect(window.location.search).not.toContain("cols=")
  })

  it("renders each valid URL column once and canonicalizes duplicate IDs", async () => {
    window.history.replaceState(
      null,
      "",
      "/?t=IREN&side=call&m=itm&cols=strike_cents,strike_cents,unknown,iv_pct_tenths",
    )
    render(<ItmChain />)

    await screen.findByRole("columnheader", { name: "IV" })
    expect(screen.getAllByRole("columnheader").map((header) => header.textContent)).toEqual([
      "Strike", "IV", "Copy",
    ])
    expect(screen.getByRole("button", { name: "Columns2" })).toBeTruthy()
    expect(window.location.search).toContain("cols=strike_cents%2Civ_pct_tenths")
  })

  it("normalizes sorting when a selected column is removed and keeps one column selected", async () => {
    window.history.replaceState(null, "", "/?t=IREN&side=call&m=itm&cols=strike_cents,iv_pct_tenths")
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByRole("columnheader", { name: "IV" })).toBeTruthy())

    fireEvent.click(screen.getByRole("button", { name: "IV" }))
    expect(screen.getByRole("columnheader", { name: "IV" }).getAttribute("aria-sort")).toBe("ascending")
    fireEvent.click(screen.getByText("Columns"))
    fireEvent.click(screen.getByRole("checkbox", { name: "IV" }))

    expect(screen.queryByRole("columnheader", { name: "IV" })).toBeNull()
    expect(screen.getByRole("columnheader", { name: "Strike" }).getAttribute("aria-sort")).toBe("descending")
    const strikeToggle = screen.getByRole("checkbox", { name: "Strike" })
    fireEvent.click(strikeToggle)
    expect(strikeToggle.getAttribute("aria-checked")).toBe("true")
    expect(screen.getByRole("columnheader", { name: "Strike" })).toBeTruthy()
    expect(window.location.search).toContain("cols=strike_cents")
  })

  it("toggles the visible fallback sort when an explicit URL omits Strike", async () => {
    window.history.replaceState(null, "", "/?t=IREN&side=call&m=itm&cols=call_bid_cents")
    render(<ItmChain />)
    const bidHeader = await screen.findByRole("columnheader", { name: "Bid" })

    expect(bidHeader.getAttribute("aria-sort")).toBe("ascending")
    fireEvent.click(screen.getByRole("button", { name: "Bid" }))
    expect(bidHeader.getAttribute("aria-sort")).toBe("descending")
  })

  it("expands and collapses all expirations without removing their headers", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-10-09/ })).toBeTruthy())
    expect(screen.getAllByRole("table")).toHaveLength(1)

    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))
    expect(screen.getAllByRole("table")).toHaveLength(2)
    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }))
    expect(screen.queryByRole("table")).toBeNull()
    expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy()
    expect(screen.getByRole("heading", { name: /2026-10-09/ })).toBeTruthy()
  })

  it("resets expansion choices whenever the ticker identity changes", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-10-09/ })).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))
    expect(screen.getAllByRole("table")).toHaveLength(2)

    await selectTicker("CIFR")
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("CIFR", "call", "itm"))
    expect(screen.getAllByRole("table")).toHaveLength(1)
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))
    expect(screen.getAllByRole("table")).toHaveLength(2)

    await selectTicker("IREN")
    await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith("IREN", "call", "itm"))
    expect(screen.getAllByRole("table")).toHaveLength(1)
  })

  it("refetches when moneyness changes and keeps the selected strategy", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("IREN", "call", "itm"))
    fireEvent.click(screen.getByRole("radio", { name: "All" }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("IREN", "call", "all"))
  })

  it("shows Greeks only after they are enabled and does not refetch", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy())
    expect(screen.queryByRole("columnheader", { name: "IV" })).toBeNull()
    fireEvent.click(screen.getByText("Columns"))
    const ivToggle = screen.getAllByLabelText(/IV/)[0]
    fireEvent.click(ivToggle)
    expect(screen.getAllByRole("columnheader", { name: "IV" }).length).toBeGreaterThan(0)
    expect(screen.getAllByText("45.0%").length).toBeGreaterThan(0)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("toggles theme into localStorage and sets the dark class", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Theme: system" }))
    expect(document.documentElement.classList.contains("dark")).toBe(true)
    expect(window.localStorage.getItem("theme")).toBe("dark")
    fireEvent.click(screen.getByRole("button", { name: "Theme: dark" }))
    expect(document.documentElement.classList.contains("dark")).toBe(false)
    expect(window.localStorage.getItem("theme")).toBe("light")
  })

  it("selects a ticker from the keyboard", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const input = screen.getByRole("combobox")
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: "CIFR" } })
    const option = await screen.findByRole("option", { name: /CIFR/ })
    fireEvent.keyDown(input, { key: "Enter" })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("CIFR", "call", "itm"))
    expect(option).toBeTruthy()
  })

  it("does not select stale ticker results during the debounce window", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const input = screen.getByRole("combobox")
    fireEvent.focus(input)
    await screen.findByRole("option", { name: /IREN/ })
    fireEvent.change(input, { target: { value: "CIFR" } })
    fireEvent.keyDown(input, { key: "Enter" })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(window.location.search).toContain("t=IREN")
  })

  it("sorts a group when a column header is clicked", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy())
    const first = screen.getByRole("heading", { name: /2026-09-18/ }).closest("section")
    const before = within(first!).getAllByRole("row").slice(1).map((row) => row.firstChild?.textContent)
    expect(before[0]).toBe("$50.00")
    fireEvent.click(within(first!).getByRole("button", { name: "Strike" }))
    const after = within(first!).getAllByRole("row").slice(1).map((row) => row.firstChild?.textContent)
    expect(after[0]).toBe("$40.50")
    expect(within(first!).getByRole("columnheader", { name: /Strike/ }).getAttribute("aria-sort")).toBe("ascending")
  })

  it("persists density in localStorage", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Density: comfortable" }))
    expect(window.localStorage.getItem("density")).toBe("compact")
    expect(document.querySelector("table")?.getAttribute("data-density")).toBe("compact")
  })
})
