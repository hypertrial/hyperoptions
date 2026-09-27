import { expect, it } from "vitest"

import { oddsLabel, predictiveAvailable, predictiveReliabilityLabel, quoteSupportLabel } from "./marketOdds"
import type { MarketOddsView, PredictiveOddsView } from "./generated/types.gen"

it("keeps real-world odds primary and accepts predictive odds only as a complete partition", () => {
  const market: MarketOddsView = { status: "available", itm_pct_tenths: 638, otm_pct_tenths: 362 }
  const predictive: PredictiveOddsView = { status: "available", itm_pct_tenths: 520, otm_pct_tenths: 470, atm_pct_tenths: 10 }
  expect(oddsLabel(market, predictive)).toMatch(/^Stock forecast 52\.0% ITM, 47\.0% OTM/)
  expect(oddsLabel(market, predictive)).not.toContain("ATM")
  expect(oddsLabel(market, predictive)).toContain("Market-implied risk-neutral 63.8% ITM")
  expect(predictiveAvailable({ ...predictive, atm_pct_tenths: 11 })).toBe(false)
  expect(oddsLabel({ status: "unavailable" }, { ...predictive, atm_pct_tenths: 11 })).toContain("Stock forecast unavailable")
})

it("labels quote tightness without treating malformed scores as evidence", () => {
  const odds: MarketOddsView = {
    status: "available", itm_pct_tenths: 638, otm_pct_tenths: 362,
    bound_low_pct_tenths: 500, bound_high_pct_tenths: 800, quote_support_score: 70,
  }
  expect(quoteSupportLabel(odds)).toBe("Quote tightness 70/100 · ITM bounds 50.0%–80.0%")
  expect(quoteSupportLabel({ ...odds, quote_support_score: 101 })).toBe("ITM bounds 50.0%–80.0%")
  expect(quoteSupportLabel({ ...odds, bound_low_pct_tenths: 900 })).toBeNull()
})

it("includes both unavailable methods in a compact accessible odds label", () => {
  expect(oddsLabel(
    { status: "unavailable", reason: "Quote bounds inconsistent" },
    { status: "unavailable", reason: "Insufficient completed history" },
  )).toContain("Stock forecast unavailable: Insufficient completed history")
})

it("shows calibration only for supported matching prospective evidence", () => {
  const forecast: PredictiveOddsView = {
    status: "available", itm_pct_tenths: 600, otm_pct_tenths: 400, atm_pct_tenths: 0,
    model_version: "ewma-v1", validation_evidence: {
      source: "prospective_as_issued", model_version: "ewma-v1", horizon_band: "6–25 sessions",
      moneyness_band: "near ATM", independent_units: 500,
      predicted_itm_pct_tenths: 600, observed_itm_pct_tenths: 570,
      through_session: "2026-09-25",
    },
  }
  expect(predictiveReliabilityLabel(forecast)).toContain("60.0% forecast vs 57.0% observed ITM · 500 independent units")
  expect(predictiveReliabilityLabel({ ...forecast, validation_evidence: { ...forecast.validation_evidence!, independent_units: 499 } })).toBe("Reliability not yet established")
  expect(predictiveReliabilityLabel({ ...forecast, validation_evidence: { ...forecast.validation_evidence!, model_version: "other" } })).toBe("Reliability not yet established")
})
