// @vitest-environment jsdom

import { cleanup, fireEvent, render as rtlRender, screen, waitFor, within } from "@testing-library/react"
import type { ReactElement } from "react"
import { BrowserRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { ApiError, fetchChain, fetchTickers } from "./api"
import { formatContractValues } from "./columns"
import { formatRowClipboard } from "./copyRow"
import { setDensity } from "./density"
import ItmChain from "./ItmChain"
import ThemeToggle from "./ThemeToggle"
import { setThemePreference } from "./theme"
import { COLUMN_HEADERS, COPY_HEADERS, largeChainPage, sampleContract, sampleIvDetails, samplePage, samplePutPage } from "./testFixtures"
import type { CoveredCallPage } from "./types"
import { addWatch } from "./watchlist/api"

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>()
  return {
    ...actual,
    fetchChain: vi.fn(),
    fetchTickers: vi.fn(),
  }
})

vi.mock("./watchlist/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./watchlist/api")>()
  return { ...actual, addWatch: vi.fn() }
})

const fetchMock = vi.mocked(fetchChain)
const tickersMock = vi.mocked(fetchTickers)
const addWatchMock = vi.mocked(addWatch)
const writeText = vi.fn().mockResolvedValue(undefined)

function render(ui: ReactElement) {
  return rtlRender(<BrowserRouter>{ui}</BrowserRouter>)
}

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
    addWatchMock.mockReset()
    writeText.mockReset()
    writeText.mockResolvedValue(undefined)
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } })
  })
  afterEach(cleanup)

  it("shows clipboard denial and clears it after a successful retry", async () => {
    writeText.mockRejectedValueOnce(new Error("Clipboard access denied"))
    render(<ItmChain />)
    const button = (await screen.findAllByRole("button", { name: /Copy row IREN/ }))[0]
    fireEvent.click(button)
    expect((await screen.findByRole("alert")).textContent).toContain("Allow clipboard access")
    fireEvent.click(button)
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull())
    expect(writeText).toHaveBeenCalledTimes(2)
    expect(screen.getByText("Copied")).toBeTruthy()
  })

  it("loads IREN first, groups expirations, and sorts strikes high to low", async () => {
    render(<ItmChain />)

    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy())
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith("IREN", "call", "itm", "lognormal_ewma", expect.any(AbortSignal))
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
    expect(within(first!).getAllByRole("row")[1].querySelector("[data-heat]")?.textContent).toBe("$50.00")
    expect(within(first!).getAllByRole("row")[2].querySelector("th")?.hasAttribute("data-heat")).toBe(false)
    expect(within(first!).getAllByRole("row")[2].querySelector("td")?.hasAttribute("data-heat")).toBe(false)
    const pricedCells = within(first!).getAllByRole("row")[1].querySelectorAll("td")
    expect(pricedCells[0].textContent).toContain("Market odds are unavailable")
    expect(pricedCells[1].textContent).toBe("$0.50")
    expect(pricedCells[1].hasAttribute("data-heat")).toBe(false)
    expect(pricedCells[4].querySelector("summary")?.textContent).toBe("45.0% · Details")
    expect(pricedCells[4].hasAttribute("data-heat")).toBe(false)
    expect(pricedCells[6].textContent).toBe("$50.00")
    expect(pricedCells[6].getAttribute("data-heat")).toBe("0.50")
    const missingRowCells = within(first!).getAllByRole("row")[2].querySelectorAll("td")
    expect(missingRowCells[6].textContent).toBe("—")
    expect(missingRowCells[6].hasAttribute("data-heat")).toBe(false)
    const missingCells = [...missingRowCells].map((cell) => cell.querySelector(".iv-midpoint")?.textContent ?? cell.textContent)
    expect(missingCells.slice(1, 10)).toEqual(["—", "—", "—", "—", "—", "—", "—", "—", "—"])
    expect(screen.queryByText("Suggested trade")).toBeNull()
    expect(screen.getByRole("button", { name: "Copy row IREN 2026-09-18 strike $50.00" })).toBeTruthy()
    expect(screen.getByText("Market open at fetch")).toBeTruthy()
    expect(screen.getByText(/Stock bid/)).toBeTruthy()
    expect(screen.getByText(/\$49\.90/)).toBeTruthy()
    expect(screen.getByText(/Sep 11, 2026 10:00 AM ET/)).toBeTruthy()
    expect(screen.queryByText(/SEP 10, 2026 3:37 PM ET/)).toBeNull()
    expect(screen.getByText("1 contract · 100 sh · $4,990.00 stock")).toBeTruthy()
    expect(screen.getByText("$49.40")).toBeTruthy()
    expect(screen.getByText("1.0%")).toBeTruthy()
    const headers = within(first!).getAllByRole("columnheader")
    expect(headers.map((header) => header.textContent)).toEqual([
      "Strike",
      "Expiry odds · EWMA lognormal",
      "Bid",
      "Ask",
      "Spread (%)",
      "IV",
      "OI",
      "Premium (net)",
      "APR (net)",
      "Breakeven",
      "% to assignment",
      "Watch",
      "Copy",
    ])
    expect(headers[5].getAttribute("title")).toContain("implied volatility")
    expect(headers[6].querySelector("abbr")?.getAttribute("title")).toBe(headers[6].getAttribute("title"))
    expect(headers[6].getAttribute("title")).toContain("open interest")
    expect(headers[7].getAttribute("title")).toContain("Time value")
    expect(headers[2].getAttribute("title")).toContain("100 × (bid − intrinsic)")
    expect(screen.getByRole("main").getAttribute("aria-busy")).toBe("false")
    expect(screen.getByRole("button", { name: "Filters" }).getAttribute("aria-expanded")).toBe("false")
    expect(screen.getByLabelText("Min Premium (net) ($)")).toBeTruthy()
    expect(screen.getByLabelText("Min APR (net) (%)")).toBeTruthy()
    expect(screen.getByLabelText("Min % to assignment (%)")).toBeTruthy()
    expect(screen.getByLabelText("Min IV (%)")).toBeTruthy()
    expect(screen.getByLabelText("Min DTE")).toBeTruthy()
    expect(screen.getByLabelText("Max DTE")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "Columns" })).toBeNull()
    expect(screen.queryByRole("button", { name: "Clear all" })).toBeNull()
    expect(screen.getByRole("radio", { name: "Covered calls" })).toBeTruthy()
    expect(screen.getByRole("radio", { name: "Cash-secured puts" })).toBeTruthy()
    expect(screen.getByRole("radio", { name: "ITM" })).toBeTruthy()
    expect(screen.getByRole("radio", { name: "OTM" })).toBeTruthy()
    expect(screen.queryByRole("radio", { name: "All" })).toBeNull()
  })

  it("shows market-implied odds, source time, and a quote-quality reason in chain rows", async () => {
    const oddsPage = page()
    Object.assign(oddsPage.expirations[0].contracts[0], {
      market_odds: {
        status: "available", itm_pct_tenths: 638, otm_pct_tenths: 362,
        bound_low_pct_tenths: 500, bound_high_pct_tenths: 800, quote_support_score: 70,
        reason: null, source: "nasdaq", fetched_at: "2026-09-17T14:00:00Z",
        session_date: "2026-09-17", model_version: "regimelib-0.1.0",
      },
    })
    Object.assign(oddsPage.expirations[0].contracts[1], {
      market_odds: {
        status: "unavailable", itm_pct_tenths: null, otm_pct_tenths: null,
        reason: "Too few reliable option quotes.", source: "nasdaq",
        fetched_at: "2026-09-17T14:00:00Z", session_date: "2026-09-17",
        model_version: "regimelib-0.1.0",
      },
    })
    fetchMock.mockResolvedValue(oddsPage)
    render(<ItmChain />)

    const rows = await screen.findAllByRole("row")
    expect(rows[1].querySelector(".odds-cell")?.textContent).toContain("63.8% ITM")
    expect(rows[1].querySelector(".odds-cell")?.textContent).toContain("36.2% OTM")
    expect(rows[1].querySelector(".odds-cell")?.textContent).toContain("Quote tightness 70/100 · ITM bounds 50.0%–80.0%")
    expect(rows[2].querySelector(".odds-cell")?.textContent).toContain("Too few reliable option quotes")
    expect(screen.getByText(/Nasdaq · last estimate Sep 17, 2026/)).toBeTruthy()
  })

  it("shows IV provenance on desktop and copies it without another request", async () => {
    const diagnosticPage = page()
    Object.assign(diagnosticPage.expirations[0].contracts[0], {
      strike_cents: 8000, call_bid_cents: 2010, call_ask_cents: 2030,
      iv_pct_tenths: 877, iv_details: sampleIvDetails(),
    })
    fetchMock.mockResolvedValue(diagnosticPage)
    render(<ItmChain />)
    const summary = await screen.findByLabelText("IV details for IREN 2026-09-18 call strike $80.00: 87.7% · Details")
    expect(summary.tagName).toBe("SUMMARY")
    expect(summary.closest("td")?.querySelector("details")?.open).toBe(false)
    fireEvent.click(summary)
    expect(screen.getByText("Quote-implied IV range")).toBeTruthy()
    expect(screen.getByText("$100.00")).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole("button", { name: "Copy row IREN 2026-09-18 strike $80.00" }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    expect(writeText.mock.calls[0][0]).toContain("Midpoint IV: 87.7%")
    expect(writeText.mock.calls[0][0]).toContain("Pricing spot: $100.00")
    expect(writeText.mock.calls[0][0]).toContain("Option-chain retrieved (UTC): 2026-09-11T20:00:01Z")
  })

  it("keeps mobile IV outside the contract trigger and metric grid", async () => {
    setDesktopViewport(false)
    const diagnosticPage = page()
    Object.assign(diagnosticPage.expirations[0].contracts[0], {
      strike_cents: 8000, call_bid_cents: 2010, call_ask_cents: 2030,
      iv_pct_tenths: 877, iv_details: sampleIvDetails(),
    })
    fetchMock.mockResolvedValue(diagnosticPage)
    render(<ItmChain />)
    const trigger = await screen.findByRole("button", { name: /Show details for IREN 2026-09-18 strike \$80\.00/ })
    expect(trigger.querySelector("details")).toBeNull()
    fireEvent.click(trigger)
    const summary = await screen.findByLabelText("IV details for IREN 2026-09-18 call strike $80.00: 87.7% · Details")
    expect(summary.closest(".mobile-row-details")).toBeTruthy()
    expect(summary.closest(".mobile-row-details > dl")).toBeNull()
    expect(summary.closest(".mobile-iv-details")).toBeTruthy()
    fireEvent.click(summary)
    expect(trigger.getAttribute("aria-expanded")).toBe("true")
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("uses a labeled historical fallback without a composite score", async () => {
    const oddsPage = page()
    oddsPage.expirations[0].contracts.forEach((row, index) => Object.assign(row, {
      market_odds: {
        status: "unavailable", itm_pct_tenths: null, otm_pct_tenths: null,
        reason: "Quote bounds inconsistent", source: "nasdaq", fetched_at: "2026-09-18T20:00:00Z",
        session_date: "2026-09-18", model_version: "test",
      },
      predictive_odds: {
        status: "available", method: "lognormal_ewma", reason: null,
        itm_pct_tenths: 521, otm_pct_tenths: 479, atm_pct_tenths: 0,
        as_of_session: "2026-09-18", expiry_session: "2026-09-18",
        model_version: "lognormal-ewma60-v1", support: 60, data_hash: "test",
      },
      hypothetical_risk: {
        status: "available", reason: null, assumed_spot_cents: 4990, assumed_bid_cents: 50,
        quote_source: "nasdaq", quote_session: "2026-09-18",
        expected_pnl_cents: index === 0 ? 2000 : -1000,
        expected_return_pct_tenths: index === 0 ? 40 : -20,
        loss_pct_tenths: index === 0 ? 300 : 700,
        p05_pnl_cents: index === 0 ? -8000 : -12000,
      },
    }))
    fetchMock.mockResolvedValue(oddsPage)
    render(<ItmChain />)

    const rows = await screen.findAllByRole("row")
    expect(rows[1].querySelector(".odds-cell")?.textContent).toContain("Stock forecast")
    expect(rows[1].querySelector(".odds-cell")?.textContent).toContain("52.1% ITM")
    expect(rows[1].querySelector(".odds-cell")?.textContent).toContain("Stock close · Sep 18, 2026")
    expect(rows[1].querySelector(".odds-cell")?.textContent).not.toContain("ATM")
    expect(rows[1].querySelector(".odds-cell")?.textContent).not.toContain("Reliability not yet established")
    expect(screen.getByText(/Selected forecast: EWMA lognormal baseline/)).toBeTruthy()
    expect(rows[1].querySelector(".odds-cell")?.textContent).toContain("Why market odds unavailable?")
    expect(screen.queryByRole("columnheader", { name: "Est P&L" })).toBeNull()
    expect(screen.queryByRole("columnheader", { name: /score/i })).toBeNull()
  })

  it("sorts expiry odds by real-world ITM probability rather than market-implied ITM", async () => {
    const oddsPage = page()
    oddsPage.expirations[0].contracts.forEach((row, index) => Object.assign(row, {
      predictive_odds: {
        status: "available", method: "lognormal_ewma", itm_pct_tenths: index === 0 ? 200 : 700,
        otm_pct_tenths: index === 0 ? 800 : 300, atm_pct_tenths: 0,
        as_of_session: "2026-09-17", expiry_session: "2026-09-18",
        model_version: "lognormal-ewma60-v1",
      },
      market_odds: {
        status: "available", itm_pct_tenths: index === 0 ? 900 : 100,
        otm_pct_tenths: index === 0 ? 100 : 900,
      },
    }))
    fetchMock.mockResolvedValue(oddsPage)
    render(<ItmChain />)
    await screen.findByRole("button", { name: "Expiry odds · EWMA lognormal" })
    fireEvent.click(screen.getByRole("button", { name: "Expiry odds · EWMA lognormal" }))
    const rows = screen.getAllByRole("row")
    expect(rows[1].firstChild?.textContent).toBe("$40.50")
    expect(rows[1].querySelector(".odds-physical")?.textContent).toContain("70.0% ITM")
    expect(rows[1].querySelector(".odds-market")?.textContent).toContain("10.0% ITM")
    expect(screen.getByRole("columnheader", { name: "Expiry odds · EWMA lognormal" }).getAttribute("aria-sort")).toBe("descending")
  })

  it("sorts the selected experimental forecast and keeps mobile model comparison outside the row trigger", async () => {
    setDesktopViewport(false)
    const oddsPage = page()
    oddsPage.expirations[0].contracts.forEach((row, index) => Object.assign(row, {
      predictive_odds: {
        method: "student_t_ewma", status: "available",
        itm_pct_tenths: index === 0 ? 200 : 700, otm_pct_tenths: index === 0 ? 800 : 300,
        atm_pct_tenths: 0, as_of_session: "2026-09-17", expiry_session: "2026-09-18",
      },
      physical_models: [
        { method: "lognormal_ewma", status: "available", itm_pct_tenths: 500, otm_pct_tenths: 500, atm_pct_tenths: 0 },
        { method: "student_t_ewma", status: "available", itm_pct_tenths: index === 0 ? 200 : 700, otm_pct_tenths: index === 0 ? 800 : 300, atm_pct_tenths: 0 },
      ],
      market_models: [{ method: "regimelib", status: "pending", reason: "quotes_pending" }],
    }))
    fetchMock.mockResolvedValue(oddsPage)
    render(<ItmChain forecastModel="student_t_ewma" />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("IREN", "call", "itm", "student_t_ewma", expect.any(AbortSignal)))
    const sort = screen.getByRole("combobox", { name: "Sort by" })
    fireEvent.change(sort, { target: { value: "predictive_itm_pct_tenths" } })
    const first = document.querySelector(".mobile-option-row")!
    expect(first.textContent).toContain("70.0% ITM")
    expect(first.textContent).toContain("Student-t EWMA")
    const trigger = first.querySelector(".mobile-row-summary")!
    const compare = first.querySelector(".mobile-model-compare button")!
    expect(trigger.contains(compare)).toBe(false)
    fireEvent.click(compare)
    expect(screen.getByRole("dialog", { name: "Compare models" }).textContent).toContain("quotes_pending")
  })

  it("includes odds in the mobile contract summary before expanding details", async () => {
    setDesktopViewport(false)
    const oddsPage = page()
    Object.assign(oddsPage.expirations[0].contracts[0], {
      market_odds: {
        status: "available", itm_pct_tenths: 638, otm_pct_tenths: 362,
        bound_low_pct_tenths: 500, bound_high_pct_tenths: 800, quote_support_score: 70,
        reason: null, source: "nasdaq", fetched_at: "2026-09-17T14:00:00Z",
        session_date: "2026-09-17", model_version: "regimelib-0.1.0",
      },
    })
    fetchMock.mockResolvedValue(oddsPage)
    render(<ItmChain />)

    const summary = await screen.findByRole("button", { name: /Show details for IREN 2026-09-18 strike \$50\.00.*63\.8% ITM/ })
    expect(summary.textContent).toContain("63.8% ITM")
    expect(summary.textContent).toContain("36.2% OTM")
    expect(summary.getAttribute("aria-label")).toContain("Quote tightness 70/100")
    expect(document.querySelector(".mobile-market-quote")?.textContent).toContain("Quote Sep 11, 2026 10:00 AM ET")
    expect(document.querySelector(".mobile-market-quote")?.textContent).toContain("Open at fetch")
  })

  it("labels a missing compact quote time instead of implying a live price", async () => {
    setDesktopViewport(false)
    fetchMock.mockResolvedValue(page({ quote_timestamp: null }))
    render(<ItmChain />)

    await screen.findByRole("heading", { name: /2026-09-18/ })
    expect(document.querySelector(".mobile-market-quote")?.textContent).toContain("Quote time unavailable")
  })

  it("adds a selected desktop contract by its server key and distinguishes an existing watch", async () => {
    const watchPage = page()
    Object.assign(watchPage.expirations[0].contracts[0], { watch_key: "opaque-contract-key", watchability_reason: null })
    Object.assign(watchPage.expirations[0].contracts[1], { watch_key: null, watchability_reason: "Adjusted option root" })
    fetchMock.mockResolvedValue(watchPage)
    addWatchMock.mockResolvedValue({ created: false, item: {} as never, job: null })
    render(<ItmChain />)

    const watch = await screen.findByRole("button", { name: "Watch IREN 2026-09-18 $50.00 strike" })
    fireEvent.click(watch)
    expect(addWatchMock).toHaveBeenCalledWith("opaque-contract-key")
    expect(await screen.findByRole("button", { name: "Already watching IREN 2026-09-18 $50.00 strike" })).toBeTruthy()
    const disabled = screen.getByRole("button", { name: /Watch IREN 2026-09-18 \$40\.50 strike: Adjusted option root/ })
    expect(disabled.hasAttribute("disabled")).toBe(true)
  })

  it("sends the selected model when creating a watch", async () => {
    const watchPage = page()
    Object.assign(watchPage.expirations[0].contracts[0], { watch_key: "opaque-contract-key", watchability_reason: null })
    fetchMock.mockResolvedValue(watchPage)
    addWatchMock.mockResolvedValue({ created: true, item: {} as never, job: null })
    render(<ItmChain forecastModel="gjr_garch_t" />)
    fireEvent.click(await screen.findByRole("button", { name: "Watch IREN 2026-09-18 $50.00 strike" }))
    expect(addWatchMock).toHaveBeenCalledWith("opaque-contract-key", "gjr_garch_t")
  })

  it("shows a mobile Watch action and per-contract request errors", async () => {
    setDesktopViewport(false)
    const watchPage = page()
    Object.assign(watchPage.expirations[0].contracts[0], { watch_key: "mobile-contract-key", watchability_reason: null })
    fetchMock.mockResolvedValue(watchPage)
    addWatchMock.mockRejectedValue(new Error("Watchlist database is busy"))
    render(<ItmChain />)

    fireEvent.click(await screen.findByRole("button", { name: /Show details for IREN 2026-09-18 strike \$50\.00/ }))
    fireEvent.click(screen.getByRole("button", { name: "Watch IREN 2026-09-18 strike $50.00" }))
    expect(addWatchMock).toHaveBeenCalledWith("mobile-contract-key")
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Watchlist database is busy")
    expect(screen.getByRole("button", { name: "Retry watch IREN 2026-09-18 strike $50.00" })).toBeTruthy()
  })

  it("keeps exact millistrikes distinct in the chain and Watch controls", async () => {
    const exactPage = page({
      expirations: [{
        expiration: "2026-09-18",
        dte: 7,
        contracts: [
          sampleContract({ strike_cents: 4001, strike_exact: "40.005", watch_key: "key-005" }),
          sampleContract({ strike_cents: 4001, strike_exact: "40.006", watch_key: "key-006" }),
        ],
      }],
    })
    fetchMock.mockResolvedValue(exactPage)
    addWatchMock.mockResolvedValue({ created: true, item: {} as never, job: null })
    render(<ItmChain />)

    const exactFive = await screen.findByRole("button", { name: "Watch IREN 2026-09-18 $40.005 strike" })
    const exactSix = screen.getByRole("button", { name: "Watch IREN 2026-09-18 $40.006 strike" })
    expect(exactFive.closest("tr")?.querySelector("th")?.textContent).toBe("$40.005")
    expect(exactSix.closest("tr")?.querySelector("th")?.textContent).toBe("$40.006")
    expect(exactSix.closest("tr")!.compareDocumentPosition(exactFive.closest("tr")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "Strike" }))
    expect(screen.getByRole("columnheader", { name: "Strike" }).getAttribute("aria-sort")).toBe("ascending")
    expect(exactFive.closest("tr")!.compareDocumentPosition(exactSix.closest("tr")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(exactFive)
    expect(addWatchMock).toHaveBeenCalledWith("key-005")
    expect(exactSix.getAttribute("aria-label")).toContain("$40.006")
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

    await waitFor(() => expect(tickersMock).toHaveBeenCalledWith("ZZ", 10, expect.any(AbortSignal)))
    release?.()
    await waitFor(() => expect(screen.getByRole("option", { name: "No matches" })).toBeTruthy())
    expect(listbox.getAttribute("aria-busy")).toBe("false")
  })

  it("sizes buy-writes from a contract count and scales net premium", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$49.40")).toBeTruthy())
    fireEvent.change(screen.getByLabelText("Contracts"), { target: { value: "2" } })
    expect(screen.getByText("2 contracts · 200 sh · $9,980.00 stock")).toBeTruthy()
    expect(screen.getByText("$100.00")).toBeTruthy()
    expect(screen.getByText("$49.40")).toBeTruthy()
    expect(screen.getByText("42.1%")).toBeTruthy()
  })

  it("treats an explicit zero contract count as invalid and keeps one contract", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$49.40")).toBeTruthy())
    fireEvent.change(screen.getByLabelText("Contracts"), { target: { value: "0" } })
    expect(screen.getByLabelText("Contracts").getAttribute("aria-invalid")).toBe("true")
    expect(screen.getByText(/Enter a whole number of 1 or more/)).toBeTruthy()
    expect(screen.getByText("$49.40")).toBeTruthy()
  })

  it("rejects a contract count that would overflow scaled money", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$49.40")).toBeTruthy())
    fireEvent.change(screen.getByLabelText("Contracts"), {
      target: { value: String(Number.MAX_SAFE_INTEGER) },
    })
    expect(screen.getByLabelText("Contracts").getAttribute("aria-invalid")).toBe("true")
    expect(screen.getByText(/too large to calculate exactly/)).toBeTruthy()
    expect(screen.getByText("$49.40")).toBeTruthy()
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
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("CIFR", "call", "itm", "lognormal_ewma", expect.any(AbortSignal)))
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
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("CIFR", "call", "itm", "lognormal_ewma", expect.any(AbortSignal)))
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
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("IREN", "call", "itm", "lognormal_ewma", expect.any(AbortSignal)))
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

  it("labels a complete Yahoo replacement with its own underlying price time", async () => {
    fetchMock.mockResolvedValue(page({
      chain_source: "yahoo",
      current_source: "yahoo_underlying",
      current_cents: 4393,
      last_trade_timestamp: "2026-09-11T20:00:00+00:00",
      quote_timestamp: null,
    }))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText(/Yahoo regular-market price/)).toBeTruthy())
    expect(screen.getByText(/2026-09-11T20:00:00\+00:00/)).toBeTruthy()
    expect(screen.queryByText(/No usable price/)).toBeNull()
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
    await waitFor(() => expect(screen.getByText(/coverage is incomplete/)).toBeTruthy())

    fireEvent.click(screen.getByRole("button", { name: "Refresh data" }))
    await waitFor(() => expect(screen.getByText(/Nasdaq unavailable/)).toBeTruthy())
    expect(screen.getByText("$49.40")).toBeTruthy()
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
    fireEvent.change(screen.getByLabelText("Min APR (net) (%)"), { target: { value: "500" } })
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
    tickersMock.mockRejectedValueOnce(new ApiError(503, "Ticker universe unavailable"))
      .mockResolvedValueOnce({ as_of: "2026-09-11T14:00:00Z", total: LISTINGS.length, results: LISTINGS })
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("Ticker list unavailable")).toBeTruthy())
    expect(screen.getByRole("combobox")).toHaveProperty("disabled", true)
    fireEvent.click(screen.getByRole("button", { name: "Retry ticker list" }))
    await waitFor(() => expect(screen.getByRole("combobox")).toHaveProperty("disabled", false))
    expect(tickersMock).toHaveBeenCalledTimes(2)
    await selectTicker("CIFR")
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("CIFR", "call", "itm", "lognormal_ewma", expect.any(AbortSignal)))
  })

  it("offers retry for a network failure instead of reporting no ticker matches", async () => {
    tickersMock.mockRejectedValueOnce(new Error("Local API unavailable"))
      .mockResolvedValueOnce({ as_of: "2026-09-11T14:00:00Z", total: LISTINGS.length, results: LISTINGS })
    render(<ItmChain />)

    expect(await screen.findByText("Ticker list unavailable")).toBeTruthy()
    expect(screen.queryByRole("option", { name: "No matches" })).toBeNull()
    expect(screen.getByRole("combobox")).toHaveProperty("disabled", true)
    fireEvent.click(screen.getByRole("button", { name: "Retry ticker list" }))
    await waitFor(() => expect(screen.getByRole("combobox")).toHaveProperty("disabled", false))
    expect(tickersMock).toHaveBeenCalledTimes(2)
  })

  it("stays blocked after a repeated ticker-universe outage", async () => {
    tickersMock.mockRejectedValue(new ApiError(503, "Ticker universe unavailable"))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("Ticker list unavailable")).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Retry ticker list" }))
    await waitFor(() => expect(tickersMock).toHaveBeenCalledTimes(2))
    expect(screen.getByRole("combobox")).toHaveProperty("disabled", true)
  })

  it("hides rows below each minimum and ANDs active filters", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$49.40")).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))

    fireEvent.change(screen.getByLabelText("Min Premium (net) ($)"), { target: { value: "100" } })
    expect(screen.getByText("$800.00")).toBeTruthy()
    expect(screen.queryByText("$49.40")).toBeNull()
    expect(screen.queryByRole("heading", { name: /2026-09-18/ })).toBeNull()
    expect(screen.getByRole("heading", { name: /2026-10-09/ })).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(1)

    fireEvent.change(screen.getByLabelText("Min Premium (net) ($)"), { target: { value: "" } })
    fireEvent.change(screen.getByLabelText("Min APR (net) (%)"), { target: { value: "50" } })
    expect(screen.getByText("89.8%")).toBeTruthy()
    expect(screen.queryByText("42.1%")).toBeNull()
    expect(screen.queryByRole("heading", { name: /2026-09-18/ })).toBeNull()

    fireEvent.change(screen.getByLabelText("Min APR (net) (%)"), { target: { value: "" } })
    fireEvent.change(screen.getByLabelText("Min % to assignment (%)"), { target: { value: "5" } })
    expect(screen.getByText("16.0%")).toBeTruthy()
    expect(screen.getByText("$42.00")).toBeTruthy()
    expect(screen.queryByText("$50.00")).toBeNull()

    fireEvent.change(screen.getByLabelText("Min APR (net) (%)"), { target: { value: "50" } })
    expect(screen.getByText("$45.00")).toBeTruthy()
    expect(screen.queryByText("$40.50")).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("treats invalid text as inactive and zero as an active floor", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getAllByText("$50.00")[0]).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))

    fireEvent.change(screen.getByLabelText("Min APR (net) (%)"), { target: { value: "abc" } })
    expect(screen.getAllByText("$50.00")[0]).toBeTruthy()
    expect(screen.getByText("$40.50")).toBeTruthy()
    expect(screen.getByLabelText("Min APR (net) (%)").getAttribute("aria-invalid")).toBe("true")
    expect(screen.getByRole("button", { name: "Clear invalid input APR (net) (%) abc" })).toBeTruthy()
    expect(screen.queryByRole("button", { name: "Remove filter APR (net) (%) abc" })).toBeNull()
    expect(screen.getByRole("button", { name: /Filters/ }).textContent).toContain("Fix 1")
    expect(screen.getByRole("button", { name: "Clear all" })).toBeTruthy()

    fireEvent.change(screen.getByLabelText("Min APR (net) (%)"), { target: { value: "10%5" } })
    expect(screen.getByLabelText("Min APR (net) (%)").getAttribute("aria-invalid")).toBe("true")
    expect(screen.getByRole("button", { name: "Clear invalid input APR (net) (%) 10%5" })).toBeTruthy()
    expect(screen.getByRole("button", { name: /Filters/ }).textContent).toContain("Fix 1")

    fireEvent.change(screen.getByLabelText("Min Premium (net) ($)"), { target: { value: "0" } })
    expect(screen.getAllByText("$50.00")[0]).toBeTruthy()
    expect(screen.queryByText("$40.50")).toBeNull()
    expect(screen.getByRole("button", { name: "Remove filter Premium (net) ($) 0" })).toBeTruthy()
    expect(screen.getByRole("button", { name: /Filters/ }).textContent).toContain("Fix 1")
  })

  it("shows removable active-filter chips and clears filters independently", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$49.40")).toBeTruthy())
    fireEvent.change(screen.getByLabelText("Min APR (net) (%)"), { target: { value: "42.1" } })
    fireEvent.change(screen.getByLabelText("Max DTE"), { target: { value: "7" } })

    expect(screen.getByRole("button", { name: "Remove filter APR (net) (%) 42.1" })).toBeTruthy()
    expect(screen.getByRole("button", { name: "Remove filter DTE ≤ 7" })).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "Remove filter APR (net) (%) 42.1" }))
    expect((screen.getByLabelText("Min APR (net) (%)") as HTMLInputElement).value).toBe("")
    expect((screen.getByLabelText("Max DTE") as HTMLInputElement).value).toBe("7")
    expect(screen.getByRole("button", { name: "Clear all" })).toBeTruthy()
  })

  it("filters contract-scaled net premium and keeps heatmap on remaining rows", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$49.40")).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))

    fireEvent.change(screen.getByLabelText("Min Premium (net) ($)"), { target: { value: "60" } })
    expect(screen.queryByText("$49.40")).toBeNull()
    expect(screen.getByText("$800.00")).toBeTruthy()

    fireEvent.change(screen.getByLabelText("Contracts"), { target: { value: "2" } })
    expect(screen.getByText("$100.00")).toBeTruthy()
    expect(screen.getByText("$1,600.00")).toBeTruthy()
    expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy()

    fireEvent.change(screen.getByLabelText("Min Premium (net) ($)"), { target: { value: "200" } })
    expect(screen.queryByText("$100.00")).toBeNull()
    expect(screen.getByText("$1,600.00")).toBeTruthy()
    expect(screen.queryByRole("heading", { name: /2026-09-18/ })).toBeNull()
    const remaining = screen.getByRole("heading", { name: /2026-10-09/ }).closest("section")
    expect(within(remaining!).getAllByRole("row")[1].querySelector("[data-heat]")?.getAttribute("data-heat")).toBe("0.50")
  })

  it("keeps filter text across ticker, refresh, and contracts, and Clear restores the chain", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))
    fireEvent.change(screen.getByLabelText("Min APR (net) (%)"), { target: { value: "50" } })
    fireEvent.change(screen.getByLabelText("Contracts"), { target: { value: "2" } })
    expect(screen.getByText("89.8%")).toBeTruthy()
    expect(screen.queryByText("42.1%")).toBeNull()

    await selectTicker("CIFR")
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("CIFR", "call", "itm", "lognormal_ewma", expect.any(AbortSignal)))
    expect((screen.getByLabelText("Min APR (net) (%)") as HTMLInputElement).value).toBe("50")
    expect((screen.getByLabelText("Contracts") as HTMLInputElement).value).toBe("2")
    expect(screen.queryByText("42.1%")).toBeNull()
    expect(screen.getByText("89.8%")).toBeTruthy()

    fireEvent.click(screen.getByRole("button", { name: "Refresh data" }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
    expect((screen.getByLabelText("Min APR (net) (%)") as HTMLInputElement).value).toBe("50")

    fireEvent.click(screen.getByRole("button", { name: "Clear all" }))
    expect((screen.getByLabelText("Min APR (net) (%)") as HTMLInputElement).value).toBe("")
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
    fireEvent.change(screen.getByLabelText("Min APR (net) (%)"), { target: { value: "500" } })
    expect(screen.getByText("No rows match the current filters.")).toBeTruthy()
    expect(screen.queryByText("No ITM calls for IREN")).toBeNull()
    expect(screen.queryByRole("table")).toBeNull()
  })

  it("keeps a row whose displayed net premium equals the typed minimum", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy())

    fireEvent.change(screen.getByLabelText("Min Premium (net) ($)"), { target: { value: "50" } })
    expect(screen.getByText("$49.40")).toBeTruthy()
    expect(screen.getByRole("button", { name: "Copy row IREN 2026-09-18 strike $50.00" })).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("hides expiries below Min DTE", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))

    fireEvent.change(screen.getByLabelText("Min DTE"), { target: { value: "abc" } })
    expect(screen.getByLabelText("Min DTE").getAttribute("aria-invalid")).toBe("true")
    expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy()
    expect(screen.getByRole("heading", { name: /2026-10-09/ })).toBeTruthy()

    fireEvent.change(screen.getByLabelText("Min DTE"), { target: { value: "14" } })
    expect(screen.queryByRole("heading", { name: /2026-09-18/ })).toBeNull()
    expect(screen.getByRole("heading", { name: /2026-10-09/ })).toBeTruthy()
    expect(screen.getByRole("button", { name: "Remove filter DTE ≥ 14" })).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "Remove filter DTE ≥ 14" }))
    expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("hides expiries beyond Max DTE", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))

    fireEvent.change(screen.getByLabelText("Max DTE"), { target: { value: "14" } })
    expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy()
    expect(screen.queryByRole("heading", { name: /2026-10-09/ })).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("hides blank and below-floor IV while keeping an exact displayed percent", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))
    expect(screen.getAllByText("45.0%").length).toBeGreaterThan(0)
    expect(screen.getByText("$40.50")).toBeTruthy()

    fireEvent.change(screen.getByLabelText("Min IV (%)"), { target: { value: "45" } })
    expect(screen.getByRole("button", { name: "Remove filter IV (%) 45" })).toBeTruthy()
    expect(screen.getAllByText("45.0%").length).toBeGreaterThan(0)
    expect(screen.queryByText("$40.50")).toBeNull()

    fireEvent.change(screen.getByLabelText("Contracts"), { target: { value: "2" } })
    expect(screen.getAllByText("45.0%").length).toBeGreaterThan(0)
    expect(screen.queryByText("$40.50")).toBeNull()

    fireEvent.change(screen.getByLabelText("Min IV (%)"), { target: { value: "45.1" } })
    expect(screen.queryByText("45.0%")).toBeNull()
    expect(screen.getByText("No rows match the current filters.")).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("maps an all-moneyness link to the side default and drops retired column params", async () => {
    window.history.replaceState(null, "", "/?t=IREN&side=call&m=all&cols=strike_cents,iv_pct_tenths")
    render(<ItmChain />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("IREN", "call", "itm", "lognormal_ewma", expect.any(AbortSignal)))
    expect(window.location.search).toBe("?t=IREN&side=call&m=itm")
    expect(screen.getByRole("columnheader", { name: "IV" })).toBeTruthy()
    expect(screen.getByRole("columnheader", { name: "Premium (net)" })).toBeTruthy()
    expect(screen.getByRole("columnheader", { name: "% to assignment" })).toBeTruthy()
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
      formatContractValues(sample.expirations[0].contracts[0], COLUMN_HEADERS),
      sample.expirations[0].contracts[0],
    ))
    expect(writeText.mock.calls[0][0]).toContain("1 contract · 100 sh")
    expect(writeText.mock.calls[0][0]).not.toMatch(/\| Copy \|/)
    expect(screen.getByText("Copied")).toBeTruthy()

    fireEvent.change(screen.getByLabelText("Contracts"), { target: { value: "2" } })
    fireEvent.click(screen.getByRole("button", { name: "Copy row IREN 2026-09-18 strike $50.00" }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2))
    const sized = writeText.mock.calls[1][0] as string
    expect(sized).toContain("IREN · 2026-09-18 · 7 DTE · Stock bid: $49.90 · 2 contracts · 200 sh")
    expect(sized).toContain("| $50.00 | $0.50 | $0.51 | 2.0% | 45.0% | 55 | $100.00 | 42.1% | $49.40 | 1.0% |")

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
    expect(document.querySelector(".results-count")?.textContent).toBe("1 contract · 1 expiration")
    expect(document.querySelector(".chain-context")?.textContent).toContain("1 contract")
    expect(document.querySelector(".chain-context")?.textContent).toContain("1 expiration")
    expect(screen.getByRole("button", { name: "Copy row CIFR 2026-09-18 strike $50.00" }).getAttribute("data-copied")).toBeNull()
    expect(screen.queryByText("Copied")).toBeNull()
  })

  it("bounds the initial DOM for a large chain and reveals more on request", async () => {
    fetchMock.mockResolvedValue(largeChainPage(400))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText(/Displaying 250 of 400 rows/)).toBeTruthy())
    expect(document.querySelectorAll("tbody tr")).toHaveLength(250)
    fireEvent.click(screen.getByRole("button", { name: "Show 150 more" }))
    expect(screen.queryByText(/Displaying/)).toBeNull()
    expect(document.querySelectorAll("tbody tr")).toHaveLength(400)
  }, 15_000)

  it("resets the reveal window when filters change", async () => {
    fetchMock.mockResolvedValue(largeChainPage(300))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText(/Displaying 250 of 300 rows/)).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Show all" }))
    expect(document.querySelectorAll("tbody tr")).toHaveLength(300)
    fireEvent.change(screen.getByLabelText("Max DTE"), { target: { value: "100" } })
    expect(screen.getByText(/Displaying 250 of 300 rows/)).toBeTruthy()
    expect(document.querySelectorAll("tbody tr")).toHaveLength(250)
  }, 15_000)

  it("refetches puts with OTM default and put filters", async () => {
    fetchMock.mockImplementation(async (_ticker, side) => (
      side === "put" ? samplePutPage() : page()
    ))
    render(<ItmChain />)
    await waitFor(() => expect(screen.getByText("$49.40")).toBeTruthy())

    fireEvent.click(screen.getByRole("radio", { name: "Cash-secured puts" }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("IREN", "put", "otm", "lognormal_ewma", expect.any(AbortSignal)))
    expect(screen.getByRole("heading", { name: "Cash-secured puts" })).toBeTruthy()
    expect(screen.getByText("Iris Energy Limited")).toBeTruthy()
    expect(screen.getByLabelText("Min Premium (net) ($)")).toBeTruthy()
    expect(screen.getByLabelText("Min APR (net) (%)")).toBeTruthy()
    expect(screen.getByLabelText("Min % to assignment (%)")).toBeTruthy()
    expect(screen.getByLabelText("Min IV (%)")).toBeTruthy()
    expect(screen.getByLabelText("Max DTE")).toBeTruthy()
    expect(screen.getByText("$45.00")).toBeTruthy()
    expect(screen.getByText("$44.20")).toBeTruthy()
    expect(screen.getByText("11.4%")).toBeTruthy()
    const headers = screen.getAllByRole("columnheader").map((header) => header.textContent)
    expect(headers).toEqual([
      "Strike",
      "Expiry odds · EWMA lognormal",
      "Bid",
      "Ask",
      "Spread (%)",
      "IV",
      "OI",
      "Premium (net)",
      "APR (net)",
      "Breakeven",
      "% to assignment",
      "Watch",
      "Copy",
    ])
  })

  it("drops a retired column query and keeps strike as the default sort", async () => {
    window.history.replaceState(null, "", "/?t=IREN&side=call&m=itm&cols=call_bid_cents")
    render(<ItmChain />)
    const strike = await screen.findByRole("columnheader", { name: "Strike" })
    expect(strike.getAttribute("aria-sort")).toBe("descending")
    expect(window.location.search).not.toContain("cols=")
    fireEvent.click(screen.getByRole("button", { name: "Bid" }))
    expect(screen.getByRole("columnheader", { name: "Bid" }).getAttribute("aria-sort")).toBe("ascending")
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
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("CIFR", "call", "itm", "lognormal_ewma", expect.any(AbortSignal)))
    expect(screen.getAllByRole("table")).toHaveLength(1)
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }))
    expect(screen.getAllByRole("table")).toHaveLength(2)

    await selectTicker("IREN")
    await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith("IREN", "call", "itm", "lognormal_ewma", expect.any(AbortSignal)))
    expect(screen.getAllByRole("table")).toHaveLength(1)
  })

  it("refetches when moneyness changes and keeps the selected strategy", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("IREN", "call", "itm", "lognormal_ewma", expect.any(AbortSignal)))
    fireEvent.click(screen.getByRole("radio", { name: "OTM" }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("IREN", "call", "otm", "lognormal_ewma", expect.any(AbortSignal)))
    expect(screen.queryByRole("radio", { name: "All" })).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it("toggles theme into localStorage and sets the dark class", async () => {
    render(<><ThemeToggle /><ItmChain /></>)
    await waitFor(() => expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy())
    fireEvent.click(screen.getByRole("button", { name: "Theme: system" }))
    expect(document.documentElement.classList.contains("dark")).toBe(true)
    expect(window.localStorage.getItem("theme")).toBe("dark")
    fireEvent.click(screen.getByRole("button", { name: "Theme: dark" }))
    expect(document.documentElement.classList.contains("dark")).toBe(false)
    expect(window.localStorage.getItem("theme")).toBe("light")
  })

  it("keeps theme and density usable when localStorage is denied", async () => {
    const original = Object.getOwnPropertyDescriptor(window, "localStorage")!
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() { throw new DOMException("Storage blocked", "SecurityError") },
    })
    try {
      render(<><ThemeToggle /><ItmChain /></>)
      await waitFor(() => expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy())
      fireEvent.click(screen.getByRole("button", { name: "Theme: system" }))
      expect(screen.getByRole("button", { name: "Theme: dark" })).toBeTruthy()
      fireEvent.click(screen.getByRole("button", { name: "Density: comfortable" }))
      expect(document.querySelector("table")?.getAttribute("data-density")).toBe("compact")
    } finally {
      cleanup()
      Object.defineProperty(window, "localStorage", original)
      setThemePreference("system")
      setDensity("comfortable")
    }
  })

  it("keeps preference changes when localStorage writes fail", async () => {
    const original = Object.getOwnPropertyDescriptor(window, "localStorage")!
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem(key: string) { return key === "theme" ? "system" : null },
        setItem() { throw new DOMException("Storage full", "QuotaExceededError") },
      },
    })
    try {
      render(<><ThemeToggle /><ItmChain /></>)
      await waitFor(() => expect(screen.getByRole("heading", { name: /2026-09-18/ })).toBeTruthy())
      fireEvent.click(screen.getByRole("button", { name: "Theme: system" }))
      expect(screen.getByRole("button", { name: "Theme: dark" })).toBeTruthy()
      fireEvent.click(screen.getByRole("button", { name: "Density: comfortable" }))
      expect(document.querySelector("table")?.getAttribute("data-density")).toBe("compact")
    } finally {
      cleanup()
      Object.defineProperty(window, "localStorage", original)
      setThemePreference("system")
      setDensity("comfortable")
    }
  })

  it("selects a ticker from the keyboard", async () => {
    render(<ItmChain />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const input = screen.getByRole("combobox")
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: "CIFR" } })
    const option = await screen.findByRole("option", { name: /CIFR/ })
    fireEvent.keyDown(input, { key: "Enter" })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("CIFR", "call", "itm", "lognormal_ewma", expect.any(AbortSignal)))
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
