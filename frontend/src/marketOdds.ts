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

export function preferredOddsKind(market: MarketOdds | null | undefined, predictive: PredictiveOdds | null | undefined): "market" | "predictive" | null {
  if (oddsAvailable(market)) return "market"
  if (predictiveAvailable(predictive)) return "predictive"
  return null
}

export function oddsMessage(odds: MarketOdds | null | undefined): string {
  if (odds?.status === "pending") return "Calculating market odds…"
  return odds?.reason || "Market odds are unavailable for this contract."
}

export function oddsLabel(market: MarketOdds | null | undefined, predictive?: PredictiveOdds | null): string {
  if (oddsAvailable(market)) return `Market-implied ${unsignedPercentTenths(market!.itm_pct_tenths)} ITM, ${unsignedPercentTenths(market!.otm_pct_tenths)} OTM`
  if (predictiveAvailable(predictive)) return `Historical predictive ${unsignedPercentTenths(predictive!.itm_pct_tenths)} ITM, ${unsignedPercentTenths(predictive!.otm_pct_tenths)} OTM, ${unsignedPercentTenths(predictive!.atm_pct_tenths)} ATM`
  return oddsMessage(market)
}

export function unavailableReasons(market: MarketOdds | null | undefined, predictive: PredictiveOdds | null | undefined): string {
  const reasons = [oddsMessage(market)]
  if (predictive?.status === "unavailable") reasons.push(`Historical forecast: ${predictive.reason || "unavailable"}`)
  return reasons.join(" ")
}

export function predictiveProvenance(odds: PredictiveOdds | null | undefined): string | null {
  if (!predictiveAvailable(odds)) return null
  const method = odds!.method === "empirical_scaled"
    ? "Volatility-scaled empirical model"
    : odds!.method === "lognormal_ewma"
      ? "EWMA lognormal model"
      : "Historical predictive model"
  return `${method} · completed stock closes through ${odds!.as_of_session ?? "unknown session"} · expiry session ${odds!.expiry_session ?? "unknown"}${odds!.support != null ? ` · support ${odds!.support}` : ""}`
}

export function oddsProvenance(odds: MarketOdds | null | undefined): string | null {
  if (!odds?.fetched_at) return null
  const fetched = new Date(odds.fetched_at)
  const stamp = Number.isNaN(fetched.valueOf()) ? odds.fetched_at : oddsTime.format(fetched)
  const source = odds.source === "yahoo" ? "Yahoo Finance" : odds.source === "nasdaq" ? "Nasdaq" : "Public quotes"
  const event = oddsAvailable(odds) ? "last estimate" : "quotes fetched"
  return `${source} · ${event} ${stamp}${odds.session_date ? ` · session ${odds.session_date}` : ""}`
}
