import { METRIC_KEYS, type ColumnDef, type ColumnId, type SizedContract } from "./columns"

export { METRIC_KEYS }

export type MetricRange = { min: number; max: number }
export type MetricKey = ColumnId
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
  const mins: Record<string, number> = {}
  const maxs: Record<string, number> = {}
  for (const row of contracts) {
    for (const column of columns) {
      if (!column.heatmap) continue
      const value = column.accessor(row)
      if (value == null || !Number.isFinite(value)) continue
      const currentMin = mins[column.id]
      const currentMax = maxs[column.id]
      mins[column.id] = currentMin == null || value < currentMin ? value : currentMin
      maxs[column.id] = currentMax == null || value > currentMax ? value : currentMax
    }
  }
  const ranges: MetricRanges = {}
  for (const column of columns) {
    if (!column.heatmap) continue
    const min = mins[column.id]
    const max = maxs[column.id]
    ranges[column.id] = min == null || max == null ? null : { min, max }
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
