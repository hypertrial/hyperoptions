import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"

import App from "./App"
import { contractSizeLabel, contractCountIsSafe, parseContractCount, scaleByContracts, shareCount, stockCapitalCents } from "./contracts"
import { COPY_HEADERS, copyRowAccessibleName, copyRowStateKey, formatContractValues, formatRowClipboard } from "./copyRow"
import { meetsMaximum, parseThreshold, passesFilters } from "./filters"
import { integer, moneyCents, percentTenths, signedE4, unsignedPercentTenths } from "./format"
import { heatmapHue, heatmapStop, metricRange, METRIC_KEYS } from "./heatmap"
import { CALL_COLUMNS, CALL_DEFAULT_COLUMN_IDS, COLUMN_HEADERS, PUT_DEFAULT_COLUMN_IDS, defaultColumnIds, mobilePriorityColumns, visibleColumns, type ColumnDef } from "./columns"
import type { CoveredCallContract } from "./types"
import { largeChainPage, missingContract, sampleContract, samplePage } from "./testFixtures"
import { deriveChainView, INITIAL_REVEAL } from "./viewModel"

describe("ITM chain page", () => {
  it("renders a searchable picker, strategy toggles, and a skip link", () => {
    const markup = renderToStaticMarkup(createElement(App))

    expect(markup).toContain("HyperOptions")
    expect(markup).not.toContain("Premium Desk")
    expect(markup).toContain("Covered calls")
    expect(markup).toContain("Option chain")
    expect(markup.match(/<h1\b/g)).toHaveLength(1)
    expect(markup.match(/<main\b/g)).toHaveLength(1)
    expect(markup).toContain('href="#main-content"')
    expect(markup).toContain('role="combobox"')
    expect(markup).not.toContain('role="tablist"')
    expect(markup).toContain("Covered calls")
    expect(markup).toContain("Cash-secured puts")
    expect(markup).toContain("ITM")
    expect(markup).toContain("OTM")
    expect(markup).toContain("All")
    expect(markup).toContain("Loading IREN market data")
    expect(markup).not.toContain("Session unavailable")
    expect(markup).toContain("Contracts")
    expect(markup).toContain('placeholder="1"')
    expect(markup).not.toContain("Budget")
    expect(markup).not.toContain("Filters")
    expect(markup).not.toContain("0 contracts")
    expect(markup).toContain("1 contract · 100 sh")
    expect(markup).toContain("Fetching the latest quote and option chain")
    expect(markup).not.toContain("Suggested trade")
    expect(markup).not.toContain("Paper ledger")
    expect(markup).not.toContain("Find a trade")
    expect(markup).not.toContain("Copy order")
    expect(markup).not.toContain("copy-order")
    expect(markup).not.toContain("api.nasdaq.com")
    const css = readFileSync(fileURLToPath(new URL("./index.css", import.meta.url)), "utf8")
    expect(css).not.toContain("overflow-wrap: anywhere")
    expect(css).toContain("overflow-wrap: break-word")
    expect(css).not.toContain("word-spacing: 0.22em")
    expect(css).toContain("grid-template-columns: 17.5rem minmax(0, 1fr)")
    expect(css).not.toMatch(/thead th \{[^}]*text-transform:\s*uppercase/s)
    expect(css).not.toMatch(/@media \(min-width: 90rem\) \{\s*table \{ min-width: 0; \}/)
    expect(markup).toContain('aria-controls="main-content"')
  })
})

describe("formatters", () => {
  it("renders missing values as em dashes and signs low comparisons", () => {
    expect(moneyCents(null)).toBe("—")
    expect(moneyCents(496_000)).toBe("$4,960.00")
    expect(percentTenths(125)).toBe("+12.5%")
    expect(percentTenths(-40)).toBe("-4.0%")
    expect(unsignedPercentTenths(156)).toBe("15.6%")
    expect(integer(null)).toBe("—")
    expect(integer(1234)).toBe("1,234")
    expect(signedE4(6368)).toBe("+0.6368")
    expect(signedE4(-176)).toBe("-0.0176")
    expect(signedE4(null)).toBe("—")
    expect(moneyCents(undefined)).toBe("—")
    expect(moneyCents(Number.NaN)).toBe("—")
    expect(moneyCents(0)).toBe("$0.00")
    expect(moneyCents(-15601)).toBe("-$156.01")
    expect(percentTenths(null)).toBe("—")
    expect(percentTenths(0)).toBe("0.0%")
    expect(percentTenths(Number.POSITIVE_INFINITY)).toBe("—")
    expect(unsignedPercentTenths(null)).toBe("—")
    expect(unsignedPercentTenths(undefined)).toBe("—")
  })
})

describe("metric heatmap", () => {
  it("maps the lowest value to red and the highest to green, per metric", () => {
    const range = metricRange([10, 20, null, 0])
    expect(range).toEqual({ min: 0, max: 20 })
    expect(heatmapStop(0, range)).toBe(0)
    expect(heatmapStop(20, range)).toBe(1)
    expect(heatmapStop(10, range)).toBe(0.5)
    expect(heatmapHue(0)).toBe(8)
    expect(heatmapHue(1)).toBe(128)
    expect(heatmapStop(null, range)).toBeNull()
    expect(heatmapStop(5, null)).toBeNull()
  })

  it("uses the midpoint when every present value is the same", () => {
    expect(heatmapStop(4, { min: 4, max: 4 })).toBe(0.5)
    expect(metricRange([null, Number.NaN])).toBeNull()
  })

  it("limits call heatmaps to decision outcomes", () => {
    expect(METRIC_KEYS).toEqual([
      "called_pnl_cents",
      "simple_apr_pct_tenths",
      "drop_to_breakeven_pct_tenths",
    ])
  })

  it("uses the focused strategy defaults", () => {
    expect(defaultColumnIds("call")).toEqual([...CALL_DEFAULT_COLUMN_IDS])
    expect(defaultColumnIds("put")).toEqual([...PUT_DEFAULT_COLUMN_IDS])
  })
})

describe("contract sizing", () => {
  it("parses whole contract counts and labels share plus stock capital", () => {
    expect(parseContractCount("")).toBeNull()
    expect(parseContractCount("2")).toBe(2)
    expect(parseContractCount(" 1,000 ")).toBe(1000)
    expect(parseContractCount("0")).toBeNull()
    expect(parseContractCount("-1")).toBeNull()
    expect(parseContractCount("1.5")).toBeNull()
    expect(parseContractCount("abc")).toBeNull()
    expect(parseContractCount("$2")).toBeNull()
    expect(parseContractCount("40%")).toBeNull()
    expect(shareCount(2)).toBe(200)
    expect(scaleByContracts(496_000, 2)).toBe(992_000)
    expect(scaleByContracts(4000, 2)).toBe(8000)
    expect(scaleByContracts(496_000, 0)).toBeNull()
    expect(scaleByContracts(2, 1.5)).toBeNull()
    expect(scaleByContracts(2, Number.MAX_SAFE_INTEGER)).toBeNull()
    expect(shareCount(Number.MAX_SAFE_INTEGER)).toBeNull()
    expect(stockCapitalCents(1, 4990)).toBe(499_000)
    expect(stockCapitalCents(2, 4990)).toBe(998_000)
    expect(stockCapitalCents(1, null)).toBeNull()
    expect(contractSizeLabel(1, null)).toBe("1 contract · 100 sh")
    expect(contractSizeLabel(1, 4990)).toBe("1 contract · 100 sh · $4,990.00 stock")
    expect(contractSizeLabel(2, 4990)).toBe("2 contracts · 200 sh · $9,980.00 stock")
    expect(contractCountIsSafe(2, samplePage())).toBe(true)
    expect(contractCountIsSafe(Number.MAX_SAFE_INTEGER, samplePage())).toBe(false)

    const lastExactCount = Math.floor(Number.MAX_SAFE_INTEGER / 499_000)
    expect(contractCountIsSafe(lastExactCount, samplePage())).toBe(true)
    expect(contractCountIsSafe(lastExactCount + 1, samplePage())).toBe(false)
  })
})

const openFilters = { primary: null, apr: null, drop: null, minDte: null, maxDte: null }

describe("row filters", () => {
  it("parses optional signed thresholds and ANDs minimums", () => {
    expect(parseThreshold("")).toBeNull()
    expect(parseThreshold("abc")).toBeNull()
    expect(parseThreshold(" 0 ")).toEqual({ value: 0n, scale: 0 })
    expect(parseThreshold("-$40")).toEqual({ value: -40n, scale: 0 })
    expect(parseThreshold("40%")).toEqual({ value: 40n, scale: 0 })
    expect(parseThreshold("1,000")).toEqual({ value: 1000n, scale: 0 })

    const priced = { called_pnl_cents: 4000, simple_apr_pct_tenths: 421, drop_to_breakeven_pct_tenths: 2, dte: 7 }
    const missing = { called_pnl_cents: null, simple_apr_pct_tenths: null, drop_to_breakeven_pct_tenths: 192, dte: 7 }
    expect(passesFilters(priced, openFilters, "call")).toBe(true)
    expect(passesFilters(priced, { ...openFilters, primary: parseThreshold("100") }, "call")).toBe(false)
    expect(passesFilters(priced, { ...openFilters, primary: parseThreshold("0") }, "call")).toBe(true)
    expect(passesFilters(missing, { ...openFilters, primary: parseThreshold("0") }, "call")).toBe(false)
    expect(passesFilters(missing, { ...openFilters, apr: parseThreshold("50"), drop: parseThreshold("5") }, "call")).toBe(false)
    expect(passesFilters(
      { called_pnl_cents: 29_000, simple_apr_pct_tenths: 898, drop_to_breakeven_pct_tenths: 102, dte: 28 },
      { ...openFilters, apr: parseThreshold("50"), drop: parseThreshold("5") },
      "call",
    )).toBe(true)
  })

  it("does not reuse parseContractCount and keeps percent points unscaled", () => {
    expect(parseContractCount("0")).toBeNull()
    expect(parseThreshold("0")).toEqual({ value: 0n, scale: 0 })
    expect(parseContractCount("-40")).toBeNull()
    expect(parseThreshold("-40")).toEqual({ value: -40n, scale: 0 })
    expect(parseContractCount("40%")).toBeNull()
    expect(parseThreshold("40%")).toEqual({ value: 40n, scale: 0 })
    expect(parseThreshold("40")).toEqual({ value: 40n, scale: 0 })
    expect(parseThreshold("40")).not.toEqual({ value: 4n, scale: 1 })
  })

  it("strips $, %, commas, and whitespace, and ignores empty leftovers", () => {
    expect(parseThreshold(" $1,000 ")).toEqual({ value: 1000n, scale: 0 })
    expect(parseThreshold(" 40 % ")).toEqual({ value: 40n, scale: 0 })
    expect(parseThreshold("+$40")).toEqual({ value: 40n, scale: 0 })
    expect(parseThreshold("   ")).toBeNull()
    expect(parseThreshold("$%,")).toBeNull()
    expect(parseThreshold("Infinity")).toBeNull()
    expect(parseThreshold("NaN")).toBeNull()
  })

  it("fails only the matching active filter for null and non-finite metrics", () => {
    const missing = { called_pnl_cents: null, simple_apr_pct_tenths: null, drop_to_breakeven_pct_tenths: 192, dte: 7 }
    const nonFinite = { called_pnl_cents: Number.NaN, simple_apr_pct_tenths: Number.POSITIVE_INFINITY, drop_to_breakeven_pct_tenths: 102, dte: 7 }
    expect(passesFilters(missing, { ...openFilters, drop: parseThreshold("5") }, "call")).toBe(true)
    expect(passesFilters(missing, { ...openFilters, apr: parseThreshold("50") }, "call")).toBe(false)
    expect(passesFilters(missing, { ...openFilters, primary: parseThreshold("0"), drop: parseThreshold("5") }, "call")).toBe(false)
    expect(passesFilters(nonFinite, { ...openFilters, primary: parseThreshold("0") }, "call")).toBe(false)
    expect(passesFilters(nonFinite, { ...openFilters, apr: parseThreshold("50") }, "call")).toBe(false)
    expect(passesFilters(nonFinite, { ...openFilters, drop: parseThreshold("5") }, "call")).toBe(true)
  })

  it("ANDs all three floors and includes the exact threshold", () => {
    const oct = { called_pnl_cents: 29_000, simple_apr_pct_tenths: 898, drop_to_breakeven_pct_tenths: 102, dte: 28 }
    const priced = { called_pnl_cents: 4000, simple_apr_pct_tenths: 421, drop_to_breakeven_pct_tenths: 2, dte: 7 }
    const negative = { called_pnl_cents: -1000, simple_apr_pct_tenths: 421, drop_to_breakeven_pct_tenths: 2, dte: 7 }
    expect(passesFilters(oct, { ...openFilters, primary: parseThreshold("100"), apr: parseThreshold("50"), drop: parseThreshold("5") }, "call")).toBe(true)
    expect(passesFilters(oct, { ...openFilters, primary: parseThreshold("100"), apr: parseThreshold("50"), drop: parseThreshold("10.2") }, "call")).toBe(true)
    expect(passesFilters(oct, { ...openFilters, primary: parseThreshold("100"), apr: parseThreshold("50"), drop: parseThreshold("10.21") }, "call")).toBe(false)
    expect(passesFilters(priced, { ...openFilters, primary: parseThreshold("40") }, "call")).toBe(true)
    expect(passesFilters(priced, { ...openFilters, primary: parseThreshold("40.01") }, "call")).toBe(false)
    expect(passesFilters(priced, { ...openFilters, primary: parseThreshold("100"), apr: parseThreshold("50"), drop: parseThreshold("5") }, "call")).toBe(false)
    expect(passesFilters(negative, { ...openFilters, primary: parseThreshold("-40") }, "call")).toBe(true)
    expect(passesFilters(negative, { ...openFilters, primary: parseThreshold("0") }, "call")).toBe(false)
  })

  it("compares Called P&L, APR, and Drop at displayed precision and keeps typed floors exact", () => {
    const displayedInclusive = {
      called_pnl_cents: 4000,
      simple_apr_pct_tenths: 421,
      drop_to_breakeven_pct_tenths: -1560,
      dte: 7,
    }
    const displayedBelow = {
      called_pnl_cents: 3999,
      simple_apr_pct_tenths: 420,
      drop_to_breakeven_pct_tenths: -1561,
      dte: 7,
    }
    expect(moneyCents(displayedInclusive.called_pnl_cents)).toBe("$40.00")
    expect(unsignedPercentTenths(displayedInclusive.simple_apr_pct_tenths)).toBe("42.1%")
    expect(unsignedPercentTenths(displayedInclusive.drop_to_breakeven_pct_tenths)).toBe("-156.0%")
    expect(passesFilters(displayedInclusive, { ...openFilters, primary: parseThreshold("40") }, "call")).toBe(true)
    expect(passesFilters(displayedInclusive, { ...openFilters, apr: parseThreshold("42.1") }, "call")).toBe(true)
    expect(passesFilters(displayedInclusive, { ...openFilters, drop: parseThreshold("-156") }, "call")).toBe(true)
    expect(moneyCents(displayedBelow.called_pnl_cents)).toBe("$39.99")
    expect(unsignedPercentTenths(displayedBelow.simple_apr_pct_tenths)).toBe("42.0%")
    expect(unsignedPercentTenths(displayedBelow.drop_to_breakeven_pct_tenths)).toBe("-156.1%")
    expect(passesFilters(displayedBelow, { ...openFilters, primary: parseThreshold("40") }, "call")).toBe(false)
    expect(passesFilters(displayedBelow, { ...openFilters, apr: parseThreshold("42.1") }, "call")).toBe(false)
    expect(passesFilters(displayedBelow, { ...openFilters, drop: parseThreshold("-156") }, "call")).toBe(false)
    expect(passesFilters(displayedInclusive, { ...openFilters, primary: parseThreshold("40.01") }, "call")).toBe(false)
    expect(passesFilters(displayedInclusive, { ...openFilters, apr: parseThreshold("42.14") }, "call")).toBe(false)
    expect(passesFilters(
      { called_pnl_cents: 4000, simple_apr_pct_tenths: 421, drop_to_breakeven_pct_tenths: 102, dte: 7 },
      { ...openFilters, drop: parseThreshold("10.21") },
      "call",
    )).toBe(false)
    expect(moneyCents(4000)).toBe("$40.00")
    expect(passesFilters(
      { called_pnl_cents: 4000, simple_apr_pct_tenths: 0, drop_to_breakeven_pct_tenths: 0, dte: 7 },
      { ...openFilters, primary: parseThreshold("40") },
      "call",
    )).toBe(true)
    expect(moneyCents(-15601)).toBe("-$156.01")
    expect(passesFilters(
      { called_pnl_cents: -15601, simple_apr_pct_tenths: 0, drop_to_breakeven_pct_tenths: 0, dte: 7 },
      { ...openFilters, primary: parseThreshold("-156") },
      "call",
    )).toBe(false)
  })

  it("applies an inclusive DTE range and ANDs it with the other floors", () => {
    const weekly = { called_pnl_cents: 4000, simple_apr_pct_tenths: 421, drop_to_breakeven_pct_tenths: 2, dte: 7 }
    const monthly = { called_pnl_cents: 29_000, simple_apr_pct_tenths: 898, drop_to_breakeven_pct_tenths: 102, dte: 28 }
    expect(meetsMaximum(7, null)).toBe(true)
    expect(meetsMaximum(7, parseThreshold("14"))).toBe(true)
    expect(meetsMaximum(28, parseThreshold("14"))).toBe(false)
    expect(meetsMaximum(null, parseThreshold("14"))).toBe(false)
    expect(passesFilters(weekly, openFilters, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, minDte: parseThreshold("7") }, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, minDte: parseThreshold("7.0") }, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, minDte: parseThreshold("8") }, "call")).toBe(false)
    expect(passesFilters(weekly, { ...openFilters, maxDte: parseThreshold("7") }, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, maxDte: parseThreshold("6") }, "call")).toBe(false)
    expect(passesFilters(monthly, { ...openFilters, minDte: parseThreshold("7"), maxDte: parseThreshold("28") }, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, minDte: parseThreshold("7"), maxDte: parseThreshold("7") }, "call")).toBe(true)
    expect(passesFilters(monthly, { ...openFilters, minDte: parseThreshold("7"), maxDte: parseThreshold("7") }, "call")).toBe(false)
    expect(passesFilters(weekly, { ...openFilters, minDte: parseThreshold("21") }, "call")).toBe(false)
    expect(passesFilters(monthly, { ...openFilters, minDte: parseThreshold("21") }, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, maxDte: parseThreshold("14") }, "call")).toBe(true)
    expect(passesFilters(monthly, { ...openFilters, maxDte: parseThreshold("14") }, "call")).toBe(false)
    expect(passesFilters(weekly, { ...openFilters, minDte: parseThreshold("40"), maxDte: parseThreshold("10") }, "call")).toBe(false)
    expect(passesFilters(monthly, { ...openFilters, minDte: parseThreshold("40"), maxDte: parseThreshold("10") }, "call")).toBe(false)
    expect(passesFilters(monthly, { ...openFilters, maxDte: parseThreshold("0") }, "call")).toBe(false)
    expect(passesFilters(monthly, { ...openFilters, minDte: parseThreshold("21"), apr: parseThreshold("50") }, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, maxDte: parseThreshold("14"), apr: parseThreshold("50") }, "call")).toBe(false)
  })

  it("uses put APR net and cushion to breakeven at exact integer precision", () => {
    const row = {
      premium_cents: 8000,
      apr_collateral_pct_tenths: 9999,
      apr_net_pct_tenths: 945,
      cushion_to_strike_pct_tenths: 9999,
      cushion_to_breakeven_pct_tenths: 114,
      dte: 7,
    }
    expect(passesFilters(row, { ...openFilters, primary: parseThreshold("80"), apr: parseThreshold("94.5"), drop: parseThreshold("11.4") }, "put")).toBe(true)
    expect(passesFilters(row, { ...openFilters, apr: parseThreshold("94.51") }, "put")).toBe(false)
    expect(passesFilters(row, { ...openFilters, drop: parseThreshold("11.41") }, "put")).toBe(false)
  })

  it("mounts only expanded expiration rows while retaining every header group", () => {
    const expanded = new Set(["2026-10-09"])
    const view = deriveChainView(samplePage(), 1, openFilters, INITIAL_REVEAL, "call", COLUMN_HEADERS, undefined, expanded)
    expect(view.visibleGroups.map((item) => item.group.expiration)).toEqual(["2026-09-18", "2026-10-09"])
    expect(view.mountedGroups.map((item) => item.group.expiration)).toEqual(["2026-10-09"])
    expect(view.expandedVisibleCount).toBe(1)
    expect(view.mountedCount).toBe(1)
  })

  it("selects mobile decision priorities and fills omitted slots in selected order", () => {
    const defaults = visibleColumns("call", null)
    expect(mobilePriorityColumns(defaults, "call").map((column) => column.id)).toEqual([
      "strike_cents",
      "call_bid_cents",
      "simple_apr_pct_tenths",
      "drop_to_breakeven_pct_tenths",
    ])
    const custom = visibleColumns("call", ["strike_cents", "premium_cents", "called_pnl_cents", "call_open_interest"])
    expect(mobilePriorityColumns(custom, "call").map((column) => column.id)).toEqual([
      "strike_cents",
      "premium_cents",
      "called_pnl_cents",
      "call_open_interest",
    ])
  })

  it("bounds a 5000-row view without changing complete counts", () => {
    const view = deriveChainView(
      largeChainPage(5000),
      1,
      { primary: null, apr: null, drop: null, minDte: null, maxDte: null },
      INITIAL_REVEAL,
      "call",
      COLUMN_HEADERS,
    )
    expect(view.visibleCount).toBe(5000)
    expect(view.mountedCount).toBe(250)
    expect(view.remainingCount).toBe(4750)
    expect(view.mountedGroups).toHaveLength(1)
    expect(view.mountedGroups[0].visible).toHaveLength(250)
    expect(view.visibleGroups[0].visible[0].strike_cents).toBe(4989)
    expect(view.visibleGroups[0].visible[249].strike_cents).toBe(4740)
    expect(view.visibleGroups[0].ranges.strike_cents).toBeUndefined()
    expect(view.visibleGroups[0].ranges.called_pnl_cents).toEqual({ min: 100, max: 5099 })
    expect(view.mountedGroups[0].ranges).toEqual(view.visibleGroups[0].ranges)
  })

  it("sorts within groups before the reveal slice and keeps unsorted heatmap ranges", () => {
    const filters = { primary: null, apr: null, drop: null, minDte: null, maxDte: null }
    const desc = deriveChainView(largeChainPage(400), 1, filters, INITIAL_REVEAL, "call", COLUMN_HEADERS)
    expect(desc.mountedGroups[0].visible[0].strike_cents).toBe(4989)
    expect(desc.mountedGroups[0].visible[249].strike_cents).toBe(4740)
    const asc = deriveChainView(
      largeChainPage(400),
      1,
      filters,
      INITIAL_REVEAL,
      "call",
      COLUMN_HEADERS,
      { id: "strike_cents", dir: "asc" },
    )
    expect(asc.mountedGroups[0].visible[0].strike_cents).toBe(4590)
    expect(asc.mountedGroups[0].visible[249].strike_cents).toBe(4839)
    expect(asc.mountedGroups[0].ranges).toEqual(desc.mountedGroups[0].ranges)
  })

  it("scales outlay and Called P&L with contracts but leaves P&L / sh one-contract", () => {
    const view = deriveChainView(
      samplePage(),
      2,
      openFilters,
      INITIAL_REVEAL,
      "call",
      COLUMN_HEADERS,
    )
    const priced = view.visibleGroups[0].visible[0] as CoveredCallContract
    expect(view.sizedContracts).toBe(2)
    expect(priced.stock_cost_cents).toBe(998_000)
    expect(priced.premium_cents).toBe(10_000)
    expect(priced.outlay_cents).toBe(992_000)
    expect(priced.called_pnl_cents).toBe(8000)
    expect(priced.called_pnl_per_share_cents).toBe(60)
    expect(priced.effective_cost_cents).toBe(4940)
    expect(priced.simple_apr_pct_tenths).toBe(421)
    expect(priced.stock_apr_pct_tenths).toBe(418)
    expect(priced.drop_to_breakeven_pct_tenths).toBe(10)
    expect(COLUMN_HEADERS.find((column) => column.id === "drop_to_breakeven_pct_tenths")?.format(priced)).toBe("1.0%")
  })

  it("formats P&L / sh from the dedicated field, not reconstructed cents", () => {
    const column = CALL_COLUMNS.find((item) => item.id === "called_pnl_per_share_cents")
    expect(column).toBeDefined()
    const row = sampleContract({
      strike_cents: 4000,
      call_bid_cents: 800,
      called_pnl_cents: 50,
      called_pnl_per_share_cents: 1,
    })
    expect(Math.trunc((row.called_pnl_cents ?? 0) / 100)).not.toBe(1)
    expect(row.strike_cents + (row.call_bid_cents ?? 0) - 4800).not.toBe(1)
    expect(column!.accessor(row)).toBe(1)
    expect(column!.format(row)).toBe("$0.01")
  })
})

const pricedContract = sampleContract()
const missingRow = missingContract()

describe("row clipboard", () => {
  it("formats displayed values, em dashes, and ChatGPT markdown with optional contract context", () => {
    expect(COLUMN_HEADERS.map((column) => column.label)).toEqual([...COPY_HEADERS])
    expect(formatContractValues(pricedContract)).toHaveLength(COLUMN_HEADERS.length)
    expect([...COPY_HEADERS]).toEqual([
      "Strike",
      "Bid",
      "Sprd %",
      "OI",
      "Premium",
      "Called P&L",
      "APR (net)",
      "Drop (BE)",
    ])
    expect(formatContractValues(pricedContract)).toEqual([
      "$50.00",
      "$0.50",
      "2.0%",
      "55",
      "$50.00",
      "$40.00",
      "42.1%",
      "1.0%",
    ])
    expect(formatContractValues(missingRow)).toEqual(["$40.50", "—", "—", "—", "—", "—", "—", "—"])
    expect(copyRowAccessibleName("IREN", "2026-09-18", "$50.00")).toBe("Copy row IREN 2026-09-18 strike $50.00")
    expect(copyRowStateKey("IREN", "2026-12-18", 5)).toBe("IREN-2026-12-18-5")
    expect(copyRowStateKey("CIFR", "2026-12-18", 5)).toBe("CIFR-2026-12-18-5")
    expect(copyRowStateKey("IREN", "2026-12-18", 5)).not.toBe(copyRowStateKey("CIFR", "2026-12-18", 5))

    const baseContext = {
      ticker: "IREN",
      expiration: "2026-09-18",
      dte: 7,
      currentSource: "Stock bid",
      currentCents: 4990,
      contracts: null,
    }
    expect(formatRowClipboard(baseContext, COPY_HEADERS, formatContractValues(pricedContract))).toBe(
      [
        "IREN · 2026-09-18 · 7 DTE · Stock bid: $49.90",
        "",
        "| Strike | Bid | Sprd % | OI | Premium | Called P&L | APR (net) | Drop (BE) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        "| $50.00 | $0.50 | 2.0% | 55 | $50.00 | $40.00 | 42.1% | 1.0% |",
      ].join("\n"),
    )
    expect(formatRowClipboard(baseContext, COPY_HEADERS, formatContractValues(missingRow))).toContain("| $40.50 | — | — | — | — | — | — | — |")
    expect(formatRowClipboard(baseContext, COPY_HEADERS, formatContractValues(pricedContract))).not.toContain("Copy")

    const sized = formatRowClipboard(
      { ...baseContext, contracts: 2 },
      COPY_HEADERS,
      formatContractValues({
        ...pricedContract,
        stock_cost_cents: 998_000,
        premium_cents: 10_000,
        outlay_cents: 992_000,
        called_pnl_cents: 8000,
      }),
    )
    expect(sized).toContain("IREN · 2026-09-18 · 7 DTE · Stock bid: $49.90 · 2 contracts · 200 sh")
    expect(sized).not.toContain("budget")
    expect(sized).toContain("| $50.00 | $0.50 | 2.0% | 55 | $100.00 | $80.00 | 42.1% | 1.0% |")
  })

  it("formats a five-contract CIFR-shaped buy-write in broker-leg terms", () => {
    const view = deriveChainView(
      samplePage({
        current_cents: 1792,
        expirations: [{
          expiration: "2026-11-20",
          dte: 63,
          contracts: [sampleContract({
            expiration: "2026-11-20",
            dte: 63,
            strike_cents: 1500,
            call_bid_cents: 440,
            call_ask_cents: 450,
            call_spread_cents: 10,
            stock_cost_cents: 179_200,
            premium_cents: 44_000,
            outlay_cents: 135_200,
            effective_cost_cents: 1352,
            called_pnl_cents: 14_800,
            called_pnl_per_share_cents: 148,
            simple_apr_pct_tenths: 634,
            stock_apr_pct_tenths: 478,
            drop_to_breakeven_pct_tenths: 246,
          })],
        }],
      }),
      5,
      openFilters,
      INITIAL_REVEAL,
      "call",
      COLUMN_HEADERS,
    )
    const row = view.visibleGroups[0].visible[0] as CoveredCallContract
    expect(formatContractValues(row, CALL_COLUMNS as unknown as ColumnDef[])).toEqual(expect.arrayContaining([
      "$8,960.00",
      "$2,200.00",
      "$6,760.00",
      "$740.00",
      "63.4%",
      "47.8%",
    ]))
    expect(CALL_COLUMNS.find((column) => column.id === "stock_cost_cents")?.format(row)).toBe("$8,960.00")
    expect(CALL_COLUMNS.find((column) => column.id === "premium_cents")?.format(row)).toBe("$2,200.00")
    expect(CALL_COLUMNS.find((column) => column.id === "outlay_cents")?.format(row)).toBe("$6,760.00")
    expect(CALL_COLUMNS.find((column) => column.id === "called_pnl_cents")?.format(row)).toBe("$740.00")
    expect(CALL_COLUMNS.find((column) => column.id === "simple_apr_pct_tenths")?.format(row)).toBe("63.4%")
    expect(CALL_COLUMNS.find((column) => column.id === "stock_apr_pct_tenths")?.format(row)).toBe("47.8%")
    expect(CALL_COLUMNS.find((column) => column.id === "drop_to_breakeven_pct_tenths")?.format(row)).toBe("24.6%")
  })
})
