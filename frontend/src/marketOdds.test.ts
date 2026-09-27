import { expect, it } from "vitest"

import { oddsLabel, preferredOddsKind, predictiveAvailable, quoteSupportLabel } from "./marketOdds"
import type { MarketOddsView, PredictiveOddsView } from "./generated/types.gen"

it("keeps market-implied odds primary and accepts predictive odds only as a complete partition", () => {
  const market: MarketOddsView = { status: "available", itm_pct_tenths: 638, otm_pct_tenths: 362 }
  const predictive: PredictiveOddsView = { status: "available", itm_pct_tenths: 520, otm_pct_tenths: 470, atm_pct_tenths: 10 }
  expect(preferredOddsKind(market, predictive)).toBe("market")
  expect(preferredOddsKind({ status: "unavailable" }, predictive)).toBe("predictive")
  expect(predictiveAvailable({ ...predictive, atm_pct_tenths: 11 })).toBe(false)
  expect(preferredOddsKind({ status: "unavailable" }, { ...predictive, atm_pct_tenths: 11 })).toBeNull()
})

it("labels quote tightness without treating malformed scores as evidence", () => {
  const odds: MarketOddsView = {
    status: "available", itm_pct_tenths: 638, otm_pct_tenths: 362,
    bound_low_pct_tenths: 500, bound_high_pct_tenths: 800, quote_support_score: 70,
  }
  expect(quoteSupportLabel(odds)).toBe("Quote tightness 70/100 · ITM bounds 50.0%–80.0%")
  expect(quoteSupportLabel({ ...odds, quote_support_score: 101 })).toBeNull()
  expect(quoteSupportLabel({ ...odds, bound_low_pct_tenths: 900 })).toBeNull()
})

it("includes both unavailable methods in a compact accessible odds label", () => {
  expect(oddsLabel(
    { status: "unavailable", reason: "Quote bounds inconsistent" },
    { status: "unavailable", reason: "Insufficient completed history" },
  )).toContain("Historical forecast: Insufficient completed history")
})
