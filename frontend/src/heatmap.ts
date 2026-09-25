import type { ColumnDef, SizedContract } from "./columns"

export type MetricRange = { min: number; max: number }
export type MetricRanges = Record<string, MetricRange | null>

export function metricRange(values: Array<number | null | undefined>): MetricRange | null {
  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY
  for (const value of values) {
    if (value == null || !Number.isFinite(value)) continue
    if (value < min) min = value
    if (value > max) max = value
  }
  return Number.isFinite(min) && Number.isFinite(max) ? { min, max } : null
}

export function metricRanges(contracts: SizedContract[], columns: ColumnDef[]): MetricRanges {
  const ranges: MetricRanges = {}
  for (const column of columns) {
    if (!column.heatmap) continue
    ranges[column.id] = metricRange(contracts.map((row) => column.accessor(row)))
  }
  return ranges
}

export function heatmapStop(
  value: number | null | undefined,
  range: MetricRange | null,
): number | null {
  if (value == null || !Number.isFinite(value) || range == null) return null
  const span = range.max - range.min
  if (span === 0) return 0.5
  return (value - range.min) / span
}

export function heatmapHue(stop: number): number {
  return 8 + stop * 120
}
