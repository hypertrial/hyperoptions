import type { SizedContract } from "./columns"
import { unsignedPercentTenths } from "./format"

export type IvRow = Pick<SizedContract, "iv_pct_tenths" | "iv_details">
export type IvFact = { label: string; value: string }

export const IV_MODEL_NOTE = "European, no-dividend Black–Scholes approximation for American equity options."
export const IV_SENSITIVITY_NOTE = "Bid/ask IV describes sensitivity to quoted prices, not a statistical confidence interval."
export const IV_DETAILS_UNAVAILABLE = "Calculation details unavailable."

const SPOT_BASES = {
  underlying_midpoint: "Underlying bid/ask midpoint",
  yahoo_regular_market_price: "Yahoo regular-market price",
  completed_session_close: "Completed-session close",
}

const PRICING_PATHS = {
  displayed_chain: "Displayed chain",
  matching_snapshot: "Matching snapshot",
}

function recorded(value: string | null | undefined): string {
  return value ?? "Not recorded"
}

function exactPrice(value: string | null | undefined): string {
  return value == null ? "Not recorded" : `$${value}`
}

function ivEstimate(
  value: number | null | undefined,
  message: string | null | undefined,
  pending = false,
): string {
  if (value != null) return unsignedPercentTenths(value)
  const state = pending ? "Pending" : "Unavailable"
  return message ? `${state} — ${message}` : state
}

/** Keep Decimal input strings intact: converting to Number would lose reproducibility. */
export function ivCalculationFacts(row: IvRow): IvFact[] {
  const details = row.iv_details
  if (!details) return []
  const pending = details.status === "pending"
  const facts: IvFact[] = [
    { label: "Bid IV", value: ivEstimate(details.bid_pct_tenths, details.bid_reason?.message, pending) },
    { label: "Midpoint IV", value: ivEstimate(row.iv_pct_tenths, details.reason?.message, pending) },
    { label: "Ask IV", value: ivEstimate(details.ask_pct_tenths, details.ask_reason?.message, pending) },
  ]
  if (details.bid_pct_tenths != null && details.ask_pct_tenths != null) {
    facts.push({ label: "Quote-implied IV range", value: `${unsignedPercentTenths(details.bid_pct_tenths)}–${unsignedPercentTenths(details.ask_pct_tenths)}` })
  }
  facts.push(
    { label: "Pricing spot", value: exactPrice(details.spot_exact) },
    { label: "Spot basis", value: details.spot_basis == null ? "Not recorded" : SPOT_BASES[details.spot_basis] },
    { label: "Strike input", value: exactPrice(details.strike_exact) },
    { label: "Bid price input", value: exactPrice(details.bid_price_exact) },
    { label: "Midpoint price input", value: exactPrice(details.mid_price_exact) },
    { label: "Ask price input", value: exactPrice(details.ask_price_exact) },
    { label: "Annual rate (decimal)", value: recorded(details.rate_exact) },
    { label: "Years to expiry", value: recorded(details.years_to_expiry_exact) },
    { label: "Chain source", value: recorded(details.chain_source) },
    { label: "Pricing path", value: details.pricing_path == null ? "Not recorded" : PRICING_PATHS[details.pricing_path] },
    { label: "Valuation time (UTC)", value: recorded(details.valuation_time) },
    { label: "Underlying quote time (UTC)", value: recorded(details.underlying_quote_time) },
    { label: "Option-chain retrieved (UTC)", value: recorded(details.option_chain_fetched_at) },
    { label: "Expiry session close (UTC)", value: recorded(details.expiry_close) },
    { label: "Quote session", value: recorded(details.quote_session_date) },
    { label: "Treasury rate session", value: recorded(details.rate_as_of_session) },
    { label: "Individual option bid/ask times", value: "Unknown" },
  )
  return facts
}

export function formatIvClipboard(row: IvRow): string {
  if (!row.iv_details) return IV_DETAILS_UNAVAILABLE
  return [
    "IV calculation",
    ...ivCalculationFacts(row).map(({ label, value }) => `${label}: ${value}`),
    IV_MODEL_NOTE,
    IV_SENSITIVITY_NOTE,
  ].join("\n")
}
