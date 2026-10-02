import { meetsScaledMaximum, meetsScaledMinimum, parseExactToken, type ExactDecimal } from "./decimal"
import { STRATEGIES } from "./strategy"
import type { Side } from "./types"

const THRESHOLD_TOKEN = /^\s*(?:[+-]?\s*\$?|\$\s*[+-]?)\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*%?\s*$/

export function parseThreshold(raw: string): ExactDecimal | null {
  if (!THRESHOLD_TOKEN.test(raw)) return null
  return parseExactToken(raw, /[$%,\s]/g)
}

export type FilterId = "premium" | "apr" | "breakeven" | "minIv" | "maxDte"

export type FilterTexts = Record<FilterId, string>

export const EMPTY_FILTER_TEXTS: FilterTexts = {
  premium: "",
  apr: "",
  breakeven: "",
  minIv: "",
  maxDte: "",
}

export type FilterFieldSpec = {
  id: FilterId
  inputId: string
  label: string
  chipLabel: string
  chipKey: string
}

export function filterFieldSpecs(side: Side): FilterFieldSpec[] {
  const metrics = STRATEGIES[side].filterMetrics
  return [
    {
      id: "premium",
      inputId: "min-premium",
      label: metrics.premium.label,
      chipLabel: metrics.premium.label.replace(/^Min /, ""),
      chipKey: "premium",
    },
    {
      id: "apr",
      inputId: "min-apr",
      label: metrics.apr.label,
      chipLabel: metrics.apr.label.replace(/^Min /, ""),
      chipKey: "apr",
    },
    {
      id: "breakeven",
      inputId: "min-breakeven",
      label: metrics.breakeven.label,
      chipLabel: metrics.breakeven.label.replace(/^Min /, ""),
      chipKey: "breakeven",
    },
    {
      id: "minIv",
      inputId: "min-iv",
      label: "Min IV (%)",
      chipLabel: "IV (%)",
      chipKey: "min-iv",
    },
    {
      id: "maxDte",
      inputId: "max-dte",
      label: "Max DTE",
      chipLabel: "DTE ≤",
      chipKey: "max-dte",
    },
  ]
}

export type FilterState = {
  premium: ExactDecimal | null
  apr: ExactDecimal | null
  breakeven: ExactDecimal | null
  minIv: ExactDecimal | null
  maxDte: ExactDecimal | null
}

export function passesFilters(
  row: Record<string, number | null | undefined>,
  filters: FilterState,
  side: Side,
): boolean {
  const metrics = STRATEGIES[side].filterMetrics
  return (
    meetsScaledMinimum(row[metrics.premium.key], filters.premium, metrics.premium.scale)
    && meetsScaledMinimum(row[metrics.apr.key], filters.apr, metrics.apr.scale)
    && meetsScaledMinimum(row[metrics.breakeven.key], filters.breakeven, metrics.breakeven.scale)
    && meetsScaledMinimum(row.iv_pct_tenths, filters.minIv, 1)
    && meetsScaledMaximum(row.dte, filters.maxDte, 0)
  )
}
