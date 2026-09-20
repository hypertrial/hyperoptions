import { meetsScaledMaximum, meetsScaledMinimum, parseExactToken, type ExactDecimal } from "./decimal"
import type { Side } from "./types"

export function parseThreshold(raw: string): ExactDecimal | null {
  return parseExactToken(raw, /[$%,\s]/g)
}

export function meetsMinimum(
  value: number | null | undefined,
  threshold: ExactDecimal | null,
  scale = 0,
): boolean {
  return meetsScaledMinimum(value, threshold, scale)
}

export function meetsMaximum(
  value: number | null | undefined,
  threshold: ExactDecimal | null,
  scale = 0,
): boolean {
  return meetsScaledMaximum(value, threshold, scale)
}

export type FilterState = {
  primary: ExactDecimal | null
  apr: ExactDecimal | null
  drop: ExactDecimal | null
  minDte: ExactDecimal | null
  maxDte: ExactDecimal | null
}

export function invertedDteRange(filters: FilterState): boolean {
  if (filters.minDte == null || filters.maxDte == null) return false
  const left = filters.minDte.value * (10n ** BigInt(filters.maxDte.scale))
  const right = filters.maxDte.value * (10n ** BigInt(filters.minDte.scale))
  return left > right
}

export function passesFilters(
  row: Record<string, number | null | undefined>,
  filters: FilterState,
  side: Side,
): boolean {
  if (invertedDteRange(filters)) return false
  const primaryKey = side === "put" ? "premium_cents" : "called_pnl_cents"
  const aprKey = side === "put" ? "apr_net_pct_tenths" : "simple_apr_pct_tenths"
  const dropKey = side === "put" ? "cushion_to_breakeven_pct_tenths" : "drop_to_breakeven_pct_tenths"
  return (
    meetsMinimum(row[primaryKey], filters.primary, 2)
    && meetsMinimum(row[aprKey], filters.apr, 1)
    && meetsMinimum(row[dropKey], filters.drop, 1)
    && meetsMinimum(row.dte, filters.minDte, 0)
    && meetsMaximum(row.dte, filters.maxDte, 0)
  )
}
