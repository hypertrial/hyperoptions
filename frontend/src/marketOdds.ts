import { unsignedPercentTenths } from "./format"
import type { MarketOddsView, PredictiveOddsView } from "./generated/types.gen"

export type MarketOdds = MarketOddsView
export type PredictiveOdds = PredictiveOddsView

const oddsTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  timeZoneName: "short",
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
})
const oddsDate = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", year: "numeric", month: "short", day: "numeric",
})

export function oddsAvailable(odds: MarketOdds | null | undefined): boolean {
  return odds?.status === "available"
    && odds.itm_pct_tenths != null && odds.otm_pct_tenths != null
    && Number.isInteger(odds.itm_pct_tenths) && Number.isInteger(odds.otm_pct_tenths)
    && odds.itm_pct_tenths >= 0 && odds.otm_pct_tenths >= 0
    && odds.itm_pct_tenths + odds.otm_pct_tenths === 1000
}

export function predictiveAvailable(odds: PredictiveOdds | null | undefined): boolean {
  const values = [odds?.itm_pct_tenths, odds?.otm_pct_tenths, odds?.atm_pct_tenths]
  return odds?.status === "available"
    && values.every((value) => value != null && Number.isInteger(value) && value >= 0)
    && (values[0]! + values[1]! + values[2]!) === 1000
}

export function quoteSupportLabel(odds: MarketOdds | null | undefined): string | null {
  const low = odds?.bound_low_pct_tenths
  const high = odds?.bound_high_pct_tenths
  const score = odds?.quote_support_score
  if (!oddsAvailable(odds) || low == null || high == null
    || !Number.isInteger(low) || !Number.isInteger(high)
    || low < 0 || low > high || high > 1000) return null
  const tightness = score != null && Number.isInteger(score) && score >= 0 && score <= 100
    ? `Quote tightness ${score}/100 · ` : ""
  return `${tightness}ITM bounds ${unsignedPercentTenths(low)}–${unsignedPercentTenths(high)}`
}

export function predictiveBasisLabel(odds: PredictiveOdds | null | undefined): string | null {
  if (!predictiveAvailable(odds)) return null
  if (odds!.price_basis !== "validated_underlying_quote" && odds!.as_of_session) {
    const session = new Date(`${odds!.as_of_session}T12:00:00Z`)
    if (!Number.isNaN(session.valueOf())) return `Stock close · ${oddsDate.format(session)}`
  }
  const time = odds!.price_as_of && !Number.isNaN(new Date(odds!.price_as_of).valueOf())
    ? oddsTime.format(new Date(odds!.price_as_of))
    : odds!.as_of_session ?? "date unavailable"
  return `${odds!.price_basis === "validated_underlying_quote" ? "Stock quote" : "Stock close"} · ${time}`
}

export function predictiveReliabilityLabel(odds: PredictiveOdds | null | undefined): string {
  const evidence = odds?.validation_evidence
  if (!predictiveAvailable(odds) || !evidence
    || evidence.source !== "prospective_as_issued"
    || evidence.model_version !== odds?.model_version
    || !Number.isInteger(evidence.independent_units) || evidence.independent_units < 500
    || !Number.isInteger(evidence.predicted_itm_pct_tenths)
    || !Number.isInteger(evidence.observed_itm_pct_tenths)
    || evidence.predicted_itm_pct_tenths < 0 || evidence.predicted_itm_pct_tenths > 1000
    || evidence.observed_itm_pct_tenths < 0 || evidence.observed_itm_pct_tenths > 1000
    || !evidence.horizon_band || !evidence.moneyness_band || !evidence.through_session) {
    return "Reliability not yet established"
  }
  return `Comparable prospective calibration (${evidence.horizon_band}, ${evidence.moneyness_band}): ${unsignedPercentTenths(evidence.predicted_itm_pct_tenths)} forecast vs ${unsignedPercentTenths(evidence.observed_itm_pct_tenths)} observed ITM · ${evidence.independent_units} independent units through ${evidence.through_session}`
}

export function oddsMessage(odds: MarketOdds | null | undefined): string {
  if (odds?.status === "pending") return "Calculating market odds…"
  return odds?.reason || "Market odds are unavailable for this contract."
}

export function oddsLabel(market: MarketOdds | null | undefined, predictive?: PredictiveOdds | null): string {
  const reliability = predictiveReliabilityLabel(predictive)
  const physical = predictiveAvailable(predictive)
    ? `Stock forecast ${unsignedPercentTenths(predictive!.itm_pct_tenths)} ITM, ${unsignedPercentTenths(predictive!.otm_pct_tenths)} OTM, ${predictiveBasisLabel(predictive)}${reliability === "Reliability not yet established" ? "" : `, ${reliability}`}`
    : `Stock forecast ${predictive?.status === "pending" ? "pending" : `unavailable: ${predictive?.reason || "no validated forecast"}`}`
  if (oddsAvailable(market)) return `${physical}. Market-implied risk-neutral ${unsignedPercentTenths(market!.itm_pct_tenths)} ITM, ${unsignedPercentTenths(market!.otm_pct_tenths)} OTM${quoteSupportLabel(market) ? `, ${quoteSupportLabel(market)}` : ""}`
  return `${physical}. ${oddsMessage(market)}`
}

export function predictiveProvenance(odds: PredictiveOdds | null | undefined): string | null {
  if (!predictiveAvailable(odds)) return null
  const method = odds!.method === "empirical_scaled"
    ? "Volatility-scaled empirical model"
    : odds!.method === "lognormal_ewma"
      ? "EWMA lognormal model"
      : "Historical predictive model"
  return `${method} · expiry session ${odds!.expiry_session ?? "unknown"}`
}

export function oddsProvenance(odds: MarketOdds | null | undefined): string | null {
  if (!odds?.fetched_at) return null
  const fetched = new Date(odds.fetched_at)
  const stamp = Number.isNaN(fetched.valueOf()) ? odds.fetched_at : oddsTime.format(fetched)
  const source = odds.source === "yahoo" ? "Yahoo Finance" : odds.source === "nasdaq" ? "Nasdaq" : "Public quotes"
  const event = oddsAvailable(odds) ? "last estimate" : "quotes fetched"
  return `${source} · ${event} ${stamp}${odds.session_date ? ` · session ${odds.session_date}` : ""}`
}
