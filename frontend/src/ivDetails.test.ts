import { describe, expect, it } from "vitest"

import { visibleColumns } from "./columns"
import { formatRowClipboard } from "./copyRow"
import { IV_MODEL_NOTE, IV_SENSITIVITY_NOTE, formatIvClipboard, ivCalculationFacts } from "./ivFacts"
import { sampleContract, sampleIvDetails, samplePage } from "./testFixtures"
import { deriveChainView, INITIAL_REVEAL } from "./viewModel"
import { parseThreshold, type FilterState } from "./filters"

describe("IV calculation facts", () => {
  it("preserves exact inputs and treats the root midpoint as canonical", () => {
    const row = sampleContract({ iv_pct_tenths: 877, iv_details: sampleIvDetails({
      spot_exact: "100.00000000000000000000000001",
      strike_exact: "80.000000000000000000000000001",
      rate_exact: "0.04000000000000000000000000001",
      pricing_path: "matching_snapshot",
    }) })
    const facts = Object.fromEntries(ivCalculationFacts(row).map(({ label, value }) => [label, value]))
    expect(facts["Midpoint IV"]).toBe("87.7%")
    expect(facts["Quote-implied IV range"]).toBe("72.1%–97.1%")
    expect(facts["Pricing spot"]).toBe("$100.00000000000000000000000001")
    expect(facts["Strike input"]).toBe("$80.000000000000000000000000001")
    expect(facts["Annual rate (decimal)"]).toBe("0.04000000000000000000000000001")
    expect(facts["Years to expiry"]).toBe("0.01917808219178082191780821918")
    expect(facts["Pricing path"]).toBe("Matching snapshot")
    expect(facts["Underlying quote time (UTC)"]).toBe("2026-09-11T20:00:00Z")
    expect(facts["Option-chain retrieved (UTC)"]).toBe("2026-09-11T20:00:01Z")
    expect(facts["Individual option bid/ask times"]).toBe("Unknown")
    expect(formatIvClipboard(row)).toContain(IV_MODEL_NOTE)
    expect(formatIvClipboard(row)).toContain(IV_SENSITIVITY_NOTE)
  })

  it("explains partial endpoints and does not invent a range or midpoint", () => {
    const row = sampleContract({ iv_pct_tenths: null, iv_details: sampleIvDetails({
      status: "unavailable",
      reason: { code: "midpoint_eligibility", message: "Midpoint has insufficient extrinsic value." },
      bid_pct_tenths: null,
      bid_reason: { code: "model_bounds", message: "Bid lies outside model bounds." },
      spot_exact: null,
      underlying_quote_time: null,
    }) })
    const facts = Object.fromEntries(ivCalculationFacts(row).map(({ label, value }) => [label, value]))
    expect(facts["Bid IV"]).toBe("Unavailable — Bid lies outside model bounds.")
    expect(facts["Midpoint IV"]).toBe("Unavailable — Midpoint has insufficient extrinsic value.")
    expect(facts["Ask IV"]).toBe("97.1%")
    expect(facts["Quote-implied IV range"]).toBeUndefined()
    expect(facts["Pricing spot"]).toBe("Not recorded")
    expect(facts["Underlying quote time (UTC)"]).toBe("Not recorded")
  })

  it.each(["bid", "ask"] as const)("keeps an available midpoint when only the %s endpoint is unavailable", (endpoint) => {
    const row = sampleContract({ iv_pct_tenths: 877, iv_details: sampleIvDetails({
      [`${endpoint}_pct_tenths`]: null,
      [`${endpoint}_reason`]: { code: "model_bounds", message: "Endpoint lies outside model bounds." },
      ...(endpoint === "bid" ? { bid_price_exact: "0" } : {}),
    }) })
    const facts = Object.fromEntries(ivCalculationFacts(row).map(({ label, value }) => [label, value]))
    expect(facts["Midpoint IV"]).toBe("87.7%")
    expect(facts[endpoint === "bid" ? "Bid IV" : "Ask IV"]).toBe("Unavailable — Endpoint lies outside model bounds.")
    expect(facts[endpoint === "bid" ? "Ask IV" : "Bid IV"]).toBe(endpoint === "bid" ? "97.1%" : "72.1%")
    expect(facts["Quote-implied IV range"]).toBeUndefined()
    expect(formatIvClipboard(row)).toContain("Endpoint lies outside model bounds.")
    expect(formatIvClipboard(row)).not.toContain("Quote-implied IV range")
  })

  it("labels absent or null diagnostics for compatible older responses", () => {
    expect(ivCalculationFacts({ iv_pct_tenths: 450 })).toEqual([])
    expect(formatIvClipboard({ iv_pct_tenths: null, iv_details: null })).toBe("Calculation details unavailable.")
  })

  it("appends IV facts after the unchanged clipboard context and table", () => {
    const row = sampleContract({ iv_pct_tenths: 877, iv_details: sampleIvDetails() })
    const context = { ticker: "IREN", expiration: row.expiration, dte: row.dte, currentSource: "Stock bid", currentCents: 9990, contracts: 1 }
    const base = formatRowClipboard(context, ["IV"], ["87.7%"])
    const withFacts = formatRowClipboard(context, ["IV"], ["87.7%"], row)
    expect(withFacts).toBe(`${base}\n\n${formatIvClipboard(row)}`)
    expect(withFacts).toContain("Stock bid: $99.90")
    expect(withFacts).toContain("Pricing spot: $100.00")
  })

  it("keeps sizing, sorting and inclusive Min IV tied to midpoint", () => {
    const low = sampleContract({ strike_cents: 4900, iv_pct_tenths: 876, iv_details: sampleIvDetails({ bid_pct_tenths: 4000 }) })
    const high = sampleContract({ iv_pct_tenths: 877, iv_details: sampleIvDetails() })
    const blank = sampleContract({ strike_cents: 4800, iv_pct_tenths: null, iv_details: sampleIvDetails({ status: "unavailable", reason: { code: "midpoint_eligibility", message: "Midpoint unavailable." } }) })
    const filters: FilterState = { premium: null, apr: null, breakeven: null, minIv: null, minDte: null, maxDte: null }
    const page = samplePage({ expirations: [{ expiration: high.expiration, dte: high.dte, contracts: [high, blank, low] }] })
    const view = deriveChainView(page, 5, filters, INITIAL_REVEAL, "call", visibleColumns("call"), { id: "iv_pct_tenths", dir: "asc" })
    expect(view.visibleGroups[0].visible.map((row) => row.iv_pct_tenths)).toEqual([876, 877, null])
    expect(view.visibleGroups[0].visible[1].iv_details).toBe(high.iv_details)
    expect(high.iv_details).toEqual(sampleIvDetails())
    const filtered = deriveChainView(page, 5, { ...filters, minIv: parseThreshold("87.7") }, INITIAL_REVEAL, "call", visibleColumns("call"))
    expect(filtered.visibleCount).toBe(1)
    expect(filtered.visibleGroups[0].visible[0].iv_pct_tenths).toBe(877)
  })
})
