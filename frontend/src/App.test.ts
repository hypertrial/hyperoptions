import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { createElement } from "react"
import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"

import App from "./App"
import { contractSizeLabel, contractCountIsSafe, parseContractCount, scaleByContracts, shareCount, stockCapitalCents } from "./contracts"
import { copyRowAccessibleName, copyRowStateKey, formatRowClipboard } from "./copyRow"
import { meetsScaledMaximum } from "./decimal"
import { parseThreshold, passesFilters } from "./filters"
import { dateTime, integer, moneyCents, percentTenths, plural, unsignedPercentTenths } from "./format"
import { heatmapHue, heatmapStop, metricRange } from "./heatmap"
import { formatContractValues, mobilePriorityColumns, visibleColumns } from "./columns"
import type { CoveredCallContract } from "./types"
import { COLUMN_HEADERS, COPY_HEADERS, largeChainPage, missingContract, sampleContract, samplePage } from "./testFixtures"
import { deriveChainView, INITIAL_REVEAL, nearestMatchingExpiration } from "./viewModel"

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
    expect(markup).not.toContain(">All<")
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
    expect(plural(1, "expiration")).toBe("expiration")
    expect(plural(0, "contract")).toBe("contracts")
    expect(dateTime(null)).toBe("—")
    expect(dateTime("not-a-date")).toBe("not-a-date")
    expect(dateTime("2026-09-11T14:00:00Z")).toMatch(/Sep 1[12], 2026/)
    const localZone = new Intl.DateTimeFormat("en-US", { timeZoneName: "short" })
      .formatToParts(new Date("2026-09-11T14:00:00Z"))
      .find((part) => part.type === "timeZoneName")?.value
    expect(dateTime("2026-09-11T14:00:00Z")).toContain(localZone)
    expect(dateTime("2026-09-11T14:00:00Z")).not.toMatch(/\d{1,2}\/\d{1,2}\/\d{4}/)
    expect(moneyCents(undefined)).toBe("—")
    expect(moneyCents(Number.NaN)).toBe("—")
    expect(moneyCents(0)).toBe("$0.00")
    expect(moneyCents(-15601)).toBe("-$156.01")
    expect(moneyCents(Number.MAX_SAFE_INTEGER)).toBe("$90,071,992,547,409.91")
    expect(moneyCents(-Number.MAX_SAFE_INTEGER)).toBe("-$90,071,992,547,409.91")
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

  it("limits heatmaps to net premium, net APR, and percent to assignment", () => {
    const heated = ["net_premium_cents", "net_apr_pct_tenths", "breakeven_change_pct_tenths"]
    expect(visibleColumns("call").filter((column) => column.heatmap).map((column) => column.id)).toEqual(heated)
    expect(visibleColumns("put").filter((column) => column.heatmap).map((column) => column.id)).toEqual(heated)
  })

  it("uses the same column labels for calls and puts", () => {
    expect(visibleColumns("call").map((column) => column.label)).toEqual(visibleColumns("put").map((column) => column.label))
    expect(visibleColumns("call").map((column) => column.id)).toEqual([
      "strike_cents",
      "call_bid_cents",
      "call_ask_cents",
      "call_spread_pct_tenths",
      "iv_pct_tenths",
      "call_open_interest",
      "net_premium_cents",
      "net_apr_pct_tenths",
      "breakeven_cents",
      "breakeven_change_pct_tenths",
    ])
    expect(visibleColumns("put").map((column) => column.id)[1]).toBe("put_bid_cents")
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
    expect(contractCountIsSafe(2, samplePage(), "call")).toBe(true)
    expect(contractCountIsSafe(Number.MAX_SAFE_INTEGER, samplePage(), "call")).toBe(false)

    const lastExactCount = Math.floor(Number.MAX_SAFE_INTEGER / 499_000)
    expect(contractCountIsSafe(lastExactCount, samplePage(), "call")).toBe(true)
    expect(contractCountIsSafe(lastExactCount + 1, samplePage(), "call")).toBe(false)
  })
})

const openFilters = { premium: null, apr: null, breakeven: null, minIv: null, minDte: null, maxDte: null }

describe("row filters", () => {
  it("parses optional signed thresholds and ANDs minimums", () => {
    expect(parseThreshold("")).toBeNull()
    expect(parseThreshold("abc")).toBeNull()
    expect(parseThreshold(" 0 ")).toEqual({ value: 0n, scale: 0 })
    expect(parseThreshold("-$40")).toEqual({ value: -40n, scale: 0 })
    expect(parseThreshold("40%")).toEqual({ value: 40n, scale: 0 })
    expect(parseThreshold("1,000")).toEqual({ value: 1000n, scale: 0 })

    const priced = { net_premium_cents: 5000, net_apr_pct_tenths: 421, breakeven_change_pct_tenths: 10, dte: 7 }
    const missing = { net_premium_cents: null, net_apr_pct_tenths: null, breakeven_change_pct_tenths: null, dte: 7 }
    expect(passesFilters(priced, openFilters, "call")).toBe(true)
    expect(passesFilters(priced, { ...openFilters, premium: parseThreshold("100") }, "call")).toBe(false)
    expect(passesFilters(priced, { ...openFilters, premium: parseThreshold("50") }, "call")).toBe(true)
    expect(passesFilters(missing, { ...openFilters, premium: parseThreshold("0") }, "call")).toBe(false)
    expect(passesFilters(missing, { ...openFilters, apr: parseThreshold("50"), breakeven: parseThreshold("5") }, "call")).toBe(false)
    expect(passesFilters(
      { net_premium_cents: 80_000, net_apr_pct_tenths: 898, breakeven_change_pct_tenths: 160, dte: 28 },
      { ...openFilters, apr: parseThreshold("50"), breakeven: parseThreshold("5") },
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

  it("rejects formatting that changes the meaning of a threshold", () => {
    for (const value of ["10%5", "1,2", "1,,000", "1 0", "40$", "%%40"]) {
      expect(parseThreshold(value)).toBeNull()
    }
    expect(parseThreshold("$-40.5")).toEqual({ value: -405n, scale: 1 })
    expect(parseThreshold("1,000.50%")).toEqual({ value: 100050n, scale: 2 })
  })

  it("fails only the matching active filter for null and non-finite metrics", () => {
    const missing = { net_premium_cents: null, net_apr_pct_tenths: null, breakeven_change_pct_tenths: 160, dte: 7 }
    const nonFinite = { net_premium_cents: Number.NaN, net_apr_pct_tenths: Number.POSITIVE_INFINITY, breakeven_change_pct_tenths: 160, dte: 7 }
    expect(passesFilters(missing, { ...openFilters, breakeven: parseThreshold("5") }, "call")).toBe(true)
    expect(passesFilters(missing, { ...openFilters, apr: parseThreshold("50") }, "call")).toBe(false)
    expect(passesFilters(missing, { ...openFilters, premium: parseThreshold("0"), breakeven: parseThreshold("5") }, "call")).toBe(false)
    expect(passesFilters(nonFinite, { ...openFilters, premium: parseThreshold("0") }, "call")).toBe(false)
    expect(passesFilters(nonFinite, { ...openFilters, apr: parseThreshold("50") }, "call")).toBe(false)
    expect(passesFilters(nonFinite, { ...openFilters, breakeven: parseThreshold("5") }, "call")).toBe(true)
  })

  it("ANDs the floors and includes the exact threshold", () => {
    const oct = { net_premium_cents: 80_000, net_apr_pct_tenths: 898, breakeven_change_pct_tenths: 160, dte: 28 }
    const priced = { net_premium_cents: 5000, net_apr_pct_tenths: 421, breakeven_change_pct_tenths: 10, dte: 7 }
    const negative = { net_premium_cents: -1000, net_apr_pct_tenths: 421, breakeven_change_pct_tenths: 10, dte: 7 }
    expect(passesFilters(oct, { ...openFilters, premium: parseThreshold("100"), apr: parseThreshold("50"), breakeven: parseThreshold("5") }, "call")).toBe(true)
    expect(passesFilters(oct, { ...openFilters, premium: parseThreshold("100"), apr: parseThreshold("50"), breakeven: parseThreshold("16") }, "call")).toBe(true)
    expect(passesFilters(oct, { ...openFilters, premium: parseThreshold("100"), apr: parseThreshold("50"), breakeven: parseThreshold("16.1") }, "call")).toBe(false)
    expect(passesFilters(priced, { ...openFilters, premium: parseThreshold("50") }, "call")).toBe(true)
    expect(passesFilters(priced, { ...openFilters, premium: parseThreshold("50.01") }, "call")).toBe(false)
    expect(passesFilters(priced, { ...openFilters, premium: parseThreshold("100"), apr: parseThreshold("50"), breakeven: parseThreshold("5") }, "call")).toBe(false)
    expect(passesFilters(negative, { ...openFilters, premium: parseThreshold("-10") }, "call")).toBe(true)
    expect(passesFilters(negative, { ...openFilters, premium: parseThreshold("0") }, "call")).toBe(false)
  })

  it("compares Premium (net), APR (net), and % to assignment at displayed precision", () => {
    const displayedInclusive = {
      net_premium_cents: 5000,
      net_apr_pct_tenths: 421,
      breakeven_change_pct_tenths: 10,
      dte: 7,
    }
    const displayedBelow = {
      net_premium_cents: 4999,
      net_apr_pct_tenths: 420,
      breakeven_change_pct_tenths: 9,
      dte: 7,
    }
    expect(moneyCents(displayedInclusive.net_premium_cents)).toBe("$50.00")
    expect(unsignedPercentTenths(displayedInclusive.net_apr_pct_tenths)).toBe("42.1%")
    expect(unsignedPercentTenths(displayedInclusive.breakeven_change_pct_tenths)).toBe("1.0%")
    expect(passesFilters(displayedInclusive, { ...openFilters, premium: parseThreshold("50") }, "call")).toBe(true)
    expect(passesFilters(displayedInclusive, { ...openFilters, apr: parseThreshold("42.1") }, "call")).toBe(true)
    expect(passesFilters(displayedInclusive, { ...openFilters, breakeven: parseThreshold("1") }, "call")).toBe(true)
    expect(moneyCents(displayedBelow.net_premium_cents)).toBe("$49.99")
    expect(unsignedPercentTenths(displayedBelow.net_apr_pct_tenths)).toBe("42.0%")
    expect(unsignedPercentTenths(displayedBelow.breakeven_change_pct_tenths)).toBe("0.9%")
    expect(passesFilters(displayedBelow, { ...openFilters, premium: parseThreshold("50") }, "call")).toBe(false)
    expect(passesFilters(displayedBelow, { ...openFilters, apr: parseThreshold("42.1") }, "call")).toBe(false)
    expect(passesFilters(displayedBelow, { ...openFilters, breakeven: parseThreshold("1") }, "call")).toBe(false)
    expect(passesFilters(displayedInclusive, { ...openFilters, premium: parseThreshold("50.01") }, "call")).toBe(false)
    expect(passesFilters(displayedInclusive, { ...openFilters, apr: parseThreshold("42.14") }, "call")).toBe(false)
    expect(moneyCents(-100)).toBe("-$1.00")
    expect(passesFilters(
      { net_premium_cents: -100, net_apr_pct_tenths: 0, breakeven_change_pct_tenths: 0, dte: 7 },
      { ...openFilters, premium: parseThreshold("0") },
      "call",
    )).toBe(false)
  })

  it("applies an inclusive maximum DTE and ANDs it with the other floors", () => {
    const weekly = { net_premium_cents: 5000, net_apr_pct_tenths: 421, breakeven_change_pct_tenths: 10, dte: 7 }
    const monthly = { net_premium_cents: 80_000, net_apr_pct_tenths: 898, breakeven_change_pct_tenths: 160, dte: 28 }
    expect(meetsScaledMaximum(7, null, 0)).toBe(true)
    expect(meetsScaledMaximum(7, parseThreshold("14"), 0)).toBe(true)
    expect(meetsScaledMaximum(28, parseThreshold("14"), 0)).toBe(false)
    expect(meetsScaledMaximum(null, parseThreshold("14"), 0)).toBe(false)
    expect(passesFilters(weekly, openFilters, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, maxDte: parseThreshold("7") }, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, maxDte: parseThreshold("7.0") }, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, maxDte: parseThreshold("6") }, "call")).toBe(false)
    expect(passesFilters(monthly, { ...openFilters, maxDte: parseThreshold("28") }, "call")).toBe(true)
    expect(passesFilters(monthly, { ...openFilters, maxDte: parseThreshold("7") }, "call")).toBe(false)
    expect(passesFilters(weekly, { ...openFilters, maxDte: parseThreshold("14") }, "call")).toBe(true)
    expect(passesFilters(monthly, { ...openFilters, maxDte: parseThreshold("14") }, "call")).toBe(false)
    expect(passesFilters(monthly, { ...openFilters, maxDte: parseThreshold("0") }, "call")).toBe(false)
    expect(passesFilters(monthly, { ...openFilters, maxDte: parseThreshold("28"), apr: parseThreshold("50") }, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, maxDte: parseThreshold("14"), apr: parseThreshold("50") }, "call")).toBe(false)
  })

  it("applies an inclusive minimum DTE and ANDs it with Max DTE and the other floors", () => {
    const weekly = { net_premium_cents: 5000, net_apr_pct_tenths: 421, breakeven_change_pct_tenths: 10, dte: 7 }
    const monthly = { net_premium_cents: 80_000, net_apr_pct_tenths: 898, breakeven_change_pct_tenths: 160, dte: 28 }
    expect(passesFilters(weekly, openFilters, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, minDte: parseThreshold("7") }, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, minDte: parseThreshold("7.0") }, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, minDte: parseThreshold("6") }, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, minDte: parseThreshold("8") }, "call")).toBe(false)
    expect(passesFilters(monthly, { ...openFilters, minDte: parseThreshold("14") }, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, minDte: parseThreshold("14") }, "call")).toBe(false)
    expect(passesFilters({ ...weekly, dte: null }, { ...openFilters, minDte: parseThreshold("0") }, "call")).toBe(false)
    expect(passesFilters(weekly, { ...openFilters, minDte: parseThreshold("7"), maxDte: parseThreshold("28") }, "call")).toBe(true)
    expect(passesFilters(monthly, { ...openFilters, minDte: parseThreshold("14"), maxDte: parseThreshold("28") }, "call")).toBe(true)
    expect(passesFilters(monthly, { ...openFilters, minDte: parseThreshold("28"), maxDte: parseThreshold("14") }, "call")).toBe(false)
    expect(passesFilters(monthly, { ...openFilters, minDte: parseThreshold("14"), apr: parseThreshold("50") }, "call")).toBe(true)
    expect(passesFilters(weekly, { ...openFilters, minDte: parseThreshold("7"), apr: parseThreshold("50") }, "call")).toBe(false)
  })

  it("selects the nearest expiry that still passes Min DTE", () => {
    expect(nearestMatchingExpiration(samplePage(), 1, openFilters, "call")).toBe("2026-09-18")
    expect(nearestMatchingExpiration(samplePage(), 1, { ...openFilters, minDte: parseThreshold("14") }, "call")).toBe("2026-10-09")
    expect(nearestMatchingExpiration(samplePage(), 1, { ...openFilters, minDte: parseThreshold("100") }, "call")).toBeNull()
  })

  it("uses the same net metrics for puts at exact integer precision", () => {
    const row = {
      net_premium_cents: 8000,
      net_apr_pct_tenths: 945,
      breakeven_change_pct_tenths: 114,
      dte: 7,
    }
    expect(passesFilters(row, { ...openFilters, premium: parseThreshold("80"), apr: parseThreshold("94.5"), breakeven: parseThreshold("11.4") }, "put")).toBe(true)
    expect(passesFilters(row, { ...openFilters, apr: parseThreshold("94.51") }, "put")).toBe(false)
    expect(passesFilters(row, { ...openFilters, breakeven: parseThreshold("11.41") }, "put")).toBe(false)
    expect(passesFilters(row, { ...openFilters, premium: parseThreshold("80.01") }, "put")).toBe(false)
  })

  it("applies an inclusive minimum IV at displayed tenths and ANDs it with other floors", () => {
    const priced = { net_premium_cents: 5000, net_apr_pct_tenths: 421, breakeven_change_pct_tenths: 10, iv_pct_tenths: 450, dte: 7 }
    const below = { ...priced, iv_pct_tenths: 449 }
    const blank = { ...priced, iv_pct_tenths: null }
    expect(unsignedPercentTenths(priced.iv_pct_tenths)).toBe("45.0%")
    expect(passesFilters(priced, openFilters, "call")).toBe(true)
    expect(passesFilters(priced, { ...openFilters, minIv: parseThreshold("45") }, "call")).toBe(true)
    expect(passesFilters(priced, { ...openFilters, minIv: parseThreshold("45.0") }, "call")).toBe(true)
    expect(passesFilters(priced, { ...openFilters, minIv: parseThreshold("45.1") }, "call")).toBe(false)
    expect(passesFilters(below, { ...openFilters, minIv: parseThreshold("45") }, "call")).toBe(false)
    expect(passesFilters(blank, openFilters, "call")).toBe(true)
    expect(passesFilters(blank, { ...openFilters, minIv: parseThreshold("0") }, "call")).toBe(false)
    expect(passesFilters(priced, { ...openFilters, minIv: parseThreshold("45"), apr: parseThreshold("50") }, "call")).toBe(false)
    expect(passesFilters(priced, { ...openFilters, minIv: parseThreshold("45"), apr: parseThreshold("42.1") }, "call")).toBe(true)
    expect(passesFilters(
      { net_premium_cents: 8000, net_apr_pct_tenths: 945, breakeven_change_pct_tenths: 114, iv_pct_tenths: 380, dte: 7 },
      { ...openFilters, minIv: parseThreshold("38") },
      "put",
    )).toBe(true)
    expect(passesFilters(
      { net_premium_cents: 8000, net_apr_pct_tenths: 945, breakeven_change_pct_tenths: 114, iv_pct_tenths: 380, dte: 7 },
      { ...openFilters, minIv: parseThreshold("38.1") },
      "put",
    )).toBe(false)
  })

  it("mounts only expanded expiration rows while retaining every header group", () => {
    const expanded = new Set(["2026-10-09"])
    const view = deriveChainView(samplePage(), 1, openFilters, INITIAL_REVEAL, "call", COLUMN_HEADERS, undefined, expanded)
    expect(view.visibleGroups.map((item) => item.group.expiration)).toEqual(["2026-09-18", "2026-10-09"])
    expect(view.mountedGroups.map((item) => item.group.expiration)).toEqual(["2026-10-09"])
    expect(view.expandedVisibleCount).toBe(1)
    expect(view.mountedCount).toBe(1)
  })

  it("selects mobile decision priorities", () => {
    const defaults = visibleColumns("call")
    expect(mobilePriorityColumns(defaults, "call").map((column) => column.id)).toEqual([
      "strike_cents",
      "call_bid_cents",
      "net_apr_pct_tenths",
      "breakeven_change_pct_tenths",
    ])
    expect(mobilePriorityColumns(visibleColumns("put"), "put").map((column) => column.id)).toEqual([
      "strike_cents",
      "put_bid_cents",
      "net_apr_pct_tenths",
      "breakeven_change_pct_tenths",
    ])
  })

  it("bounds a 5000-row view without changing complete counts", () => {
    const view = deriveChainView(
      largeChainPage(5000),
      1,
      openFilters,
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
    expect(view.visibleGroups[0].ranges.net_premium_cents).toEqual({ min: 100, max: 5099 })
    expect(view.mountedGroups[0].ranges).toEqual(view.visibleGroups[0].ranges)
  })

  it("sorts within groups before the reveal slice and keeps unsorted heatmap ranges", () => {
    const desc = deriveChainView(largeChainPage(400), 1, openFilters, INITIAL_REVEAL, "call", COLUMN_HEADERS)
    expect(desc.mountedGroups[0].visible[0].strike_cents).toBe(4989)
    expect(desc.mountedGroups[0].visible[249].strike_cents).toBe(4740)
    const asc = deriveChainView(
      largeChainPage(400),
      1,
      openFilters,
      INITIAL_REVEAL,
      "call",
      COLUMN_HEADERS,
      { id: "strike_cents", dir: "asc" },
    )
    expect(asc.mountedGroups[0].visible[0].strike_cents).toBe(4590)
    expect(asc.mountedGroups[0].visible[249].strike_cents).toBe(4839)
    expect(asc.mountedGroups[0].ranges).toEqual(desc.mountedGroups[0].ranges)
  })

  it("scales net premium with contracts and leaves bid, APR, and breakeven one-contract", () => {
    const view = deriveChainView(
      samplePage(),
      2,
      openFilters,
      INITIAL_REVEAL,
      "call",
      COLUMN_HEADERS,
    )
    const priced = view.visibleGroups[0].visible[0] as CoveredCallContract
    expect(priced.net_premium_cents).toBe(10_000)
    expect(priced.stock_cost_cents).toBe(998_000)
    expect(priced.call_bid_cents).toBe(50)
    expect(priced.breakeven_cents).toBe(4940)
    expect(priced.net_apr_pct_tenths).toBe(421)
    expect(priced.breakeven_change_pct_tenths).toBe(10)
    expect(COLUMN_HEADERS.find((column) => column.id === "net_premium_cents")?.format(priced)).toBe("$100.00")
    expect(COLUMN_HEADERS.find((column) => column.id === "breakeven_cents")?.format(priced)).toBe("$49.40")
    expect(COLUMN_HEADERS.find((column) => column.id === "breakeven_change_pct_tenths")?.format(priced)).toBe("1.0%")
  })
})

const pricedContract = sampleContract()
const missingRow = missingContract()
const clipboardHeaders = [
  "Strike",
  "Bid",
  "Ask",
  "Spread (%)",
  "IV",
  "OI",
  "Premium (net)",
  "APR (net)",
  "Breakeven",
  "% to assignment",
]
const pricedValues = ["$50.00", "$0.50", "$0.51", "2.0%", "45.0%", "55", "$50.00", "42.1%", "$49.40", "1.0%"]

describe("row clipboard", () => {
  it("formats displayed values, em dashes, and ChatGPT markdown with optional contract context", () => {
    expect(COLUMN_HEADERS.map((column) => column.label)).toEqual([...COPY_HEADERS])
    expect([...COPY_HEADERS]).toEqual(clipboardHeaders)
    expect(formatContractValues(pricedContract, COLUMN_HEADERS)).toEqual(pricedValues)
    expect(formatContractValues(missingRow, COLUMN_HEADERS)).toEqual(["$40.50", "—", "—", "—", "—", "—", "—", "—", "—", "—"])
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
    expect(formatRowClipboard(baseContext, COPY_HEADERS, formatContractValues(pricedContract, COLUMN_HEADERS))).toBe(
      [
        "IREN · 2026-09-18 · 7 DTE · Stock bid: $49.90",
        "",
        "| Strike | Bid | Ask | Spread (%) | IV | OI | Premium (net) | APR (net) | Breakeven | % to assignment |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        "| $50.00 | $0.50 | $0.51 | 2.0% | 45.0% | 55 | $50.00 | 42.1% | $49.40 | 1.0% |",
      ].join("\n"),
    )
    expect(formatRowClipboard(baseContext, COPY_HEADERS, formatContractValues(missingRow, COLUMN_HEADERS))).toContain("| $40.50 | — | — | — | — | — | — | — | — | — |")
    expect(formatRowClipboard(baseContext, COPY_HEADERS, formatContractValues(pricedContract, COLUMN_HEADERS))).not.toContain("Copy")

    const sized = formatRowClipboard(
      { ...baseContext, contracts: 2 },
      COPY_HEADERS,
      formatContractValues({
        ...pricedContract,
        net_premium_cents: 10_000,
      }, COLUMN_HEADERS),
    )
    expect(sized).toContain("IREN · 2026-09-18 · 7 DTE · Stock bid: $49.90 · 2 contracts · 200 sh")
    expect(sized).not.toContain("budget")
    expect(sized).toContain("| $50.00 | $0.50 | $0.51 | 2.0% | 45.0% | 55 | $100.00 | 42.1% | $49.40 | 1.0% |")
  })

  it("scales net premium for a five-contract row and leaves the other chain columns one-contract", () => {
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
            net_premium_cents: 44_000,
            net_apr_pct_tenths: 634,
            breakeven_cents: 1352,
            breakeven_change_pct_tenths: 246,
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
    expect(formatContractValues(row, COLUMN_HEADERS)).toEqual([
      "$15.00",
      "$4.40",
      "$4.50",
      "2.0%",
      "45.0%",
      "55",
      "$2,200.00",
      "63.4%",
      "$13.52",
      "24.6%",
    ])
  })
})
