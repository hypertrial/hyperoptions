import { unsignedPercentTenths } from "./format"
import type { MarketOddsView } from "./generated/types.gen"

export type MarketOdds = MarketOddsView

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

export function oddsMessage(odds: MarketOdds | null | undefined): string {
  if (odds?.status === "pending") return "Calculating market odds…"
  return odds?.reason || "Market odds are unavailable for this contract."
}

export function oddsLabel(odds: MarketOdds | null | undefined): string {
  return oddsAvailable(odds)
    ? `${unsignedPercentTenths(odds!.itm_pct_tenths)} ITM, ${unsignedPercentTenths(odds!.otm_pct_tenths)} OTM`
    : oddsMessage(odds)
}

export function oddsProvenance(odds: MarketOdds | null | undefined): string | null {
  if (!odds?.fetched_at) return null
  const fetched = new Date(odds.fetched_at)
  const stamp = Number.isNaN(fetched.valueOf()) ? odds.fetched_at : oddsTime.format(fetched)
  const source = odds.source === "yahoo" ? "Yahoo Finance" : odds.source === "nasdaq" ? "Nasdaq" : "Public quotes"
  const event = oddsAvailable(odds) ? "last estimate" : "quotes fetched"
  return `${source} · ${event} ${stamp}${odds.session_date ? ` · session ${odds.session_date}` : ""}`
}
