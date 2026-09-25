import { meetsScaledMaximum, meetsScaledMinimum, parseExactToken, type ExactDecimal } from "./decimal"
import { STRATEGIES } from "./strategy"
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

export type FilterId = "primary" | "apr" | "drop" | "minDte" | "maxDte"

export type FilterTexts = Record<FilterId, string>

export const EMPTY_FILTER_TEXTS: FilterTexts = {
  primary: "",
  apr: "",
  drop: "",
  minDte: "",
  maxDte: "",
}

export type FilterFieldSpec = {
  id: FilterId
  inputId: string
  label: string
  chipLabel: string
  chipKey: string
  bound: "min" | "max"
  scale: number
  rowKey: string
}

export function filterFieldSpecs(side: Side): FilterFieldSpec[] {
  const metrics = STRATEGIES[side].filterMetrics
  return [
    {
      id: "primary",
      inputId: "min-primary",
      label: metrics.primary.label,
      chipLabel: metrics.primary.label.replace(/^Min /, ""),
      chipKey: "primary",
      bound: "min",
      scale: metrics.primary.scale,
      rowKey: metrics.primary.key,
    },
    {
      id: "apr",
      inputId: "min-apr",
      label: metrics.apr.label,
      chipLabel: metrics.apr.label.replace(/^Min /, ""),
      chipKey: "apr",
      bound: "min",
      scale: metrics.apr.scale,
      rowKey: metrics.apr.key,
    },
    {
      id: "drop",
      inputId: "min-drop",
      label: metrics.drop.label,
      chipLabel: metrics.drop.label.replace(/^Min /, ""),
      chipKey: "drop",
      bound: "min",
      scale: metrics.drop.scale,
      rowKey: metrics.drop.key,
    },
    {
      id: "minDte",
      inputId: "min-dte",
      label: "Min DTE",
      chipLabel: "DTE ≥",
      chipKey: "min-dte",
      bound: "min",
      scale: 0,
      rowKey: "dte",
    },
    {
      id: "maxDte",
      inputId: "max-dte",
      label: "Max DTE",
      chipLabel: "DTE ≤",
      chipKey: "max-dte",
      bound: "max",
      scale: 0,
      rowKey: "dte",
    },
  ]
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
  inverted = invertedDteRange(filters),
): boolean {
  if (inverted) return false
  const metrics = STRATEGIES[side].filterMetrics
  return (
    meetsMinimum(row[metrics.primary.key], filters.primary, metrics.primary.scale)
    && meetsMinimum(row[metrics.apr.key], filters.apr, metrics.apr.scale)
    && meetsMinimum(row[metrics.drop.key], filters.drop, metrics.drop.scale)
    && meetsMinimum(row.dte, filters.minDte, 0)
    && meetsMaximum(row.dte, filters.maxDte, 0)
  )
}
