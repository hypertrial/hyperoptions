import type { ColumnDef, ColumnId, SizedContract } from "./columns"
import { scaleByContracts } from "./contracts"
import { parseExactDecimal } from "./decimal"
import { STRATEGIES } from "./strategy"
import { invertedDteRange, passesFilters, type FilterState } from "./filters"
import { metricRanges, type MetricRanges } from "./heatmap"
import type { ChainPage, Side } from "./types"

export type SortState = { id: ColumnId; dir: "asc" | "desc" }

export const DEFAULT_SORT: SortState = { id: "strike_cents", dir: "desc" }

export const INITIAL_REVEAL = 250

export type ExpiryGroup = {
  expiration: string
  dte: number
  contracts: SizedContract[]
}

export type VisibleGroup = {
  group: ExpiryGroup
  visible: SizedContract[]
  ranges: MetricRanges
}

export type ChainView = {
  providerCount: number
  visibleCount: number
  visibleGroups: VisibleGroup[]
  mountedGroups: VisibleGroup[]
  mountedCount: number
  expandedVisibleCount: number
  remainingCount: number
  filterMiss: boolean
  invertedDte: boolean
}

function scaleRow(row: SizedContract, contracts: number, side: Side): SizedContract {
  const scaled: Record<string, unknown> = { ...row }
  for (const field of STRATEGIES[side].scaledFields) {
    scaled[field] = scaleByContracts(scaled[field] as number | null | undefined, contracts)
  }
  return scaled as SizedContract
}

export function nearestMatchingExpiration(
  page: ChainPage | null,
  contracts: number,
  filters: FilterState,
  side: Side,
): string | null {
  const inverted = invertedDteRange(filters)
  let nearest: ChainPage["expirations"][number] | null = null
  for (const group of page?.expirations ?? []) {
    const matches = group.contracts.some((row) => (
      passesFilters(
        scaleRow(row as SizedContract, contracts, side) as unknown as Record<string, number | null>,
        filters,
        side,
        inverted,
      )
    ))
    if (matches && (nearest == null || group.dte < nearest.dte)) nearest = group
  }
  return nearest?.expiration ?? null
}

function sortRows(rows: SizedContract[], columns: ColumnDef[], sort: SortState): SizedContract[] {
  const column = columns.find((item) => item.id === sort.id)
  if (!column) return rows
  return [...rows].sort((left, right) => {
    if (sort.id === "strike_cents") {
      const leftStrike = parseExactDecimal(left.strike_exact ?? "")
      const rightStrike = parseExactDecimal(right.strike_exact ?? "")
      if (leftStrike && rightStrike) {
        const a = leftStrike.value * 10n ** BigInt(rightStrike.scale)
        const b = rightStrike.value * 10n ** BigInt(leftStrike.scale)
        const compared = a < b ? -1 : a > b ? 1 : 0
        return sort.dir === "asc" ? compared : -compared
      }
    }
    const leftValue = column.accessor(left)
    const rightValue = column.accessor(right)
    if (leftValue == null && rightValue == null) return 0
    if (leftValue == null) return 1
    if (rightValue == null) return -1
    const compared = leftValue < rightValue ? -1 : leftValue > rightValue ? 1 : 0
    return sort.dir === "asc" ? compared : -compared
  })
}

export function deriveChainView(
  page: ChainPage | null,
  contracts: number,
  filters: FilterState,
  revealLimit: number,
  side: Side,
  columns: ColumnDef[],
  sort: SortState = DEFAULT_SORT,
  expandedExpirations?: ReadonlySet<string>,
): ChainView {
  const invertedDte = invertedDteRange(filters)
  const providerCount = page?.expirations.reduce((total, group) => total + group.contracts.length, 0) ?? 0
  const visibleGroups: VisibleGroup[] = []
  if (page) {
    for (const group of page.expirations) {
      const visible: SizedContract[] = []
      for (const row of group.contracts) {
        const sized = scaleRow(row as SizedContract, contracts, side)
        if (passesFilters(sized as unknown as Record<string, number | null>, filters, side, invertedDte)) {
          visible.push(sized)
        }
      }
      if (visible.length === 0) continue
      visibleGroups.push({
        group,
        visible: sortRows(visible, columns, sort),
        ranges: metricRanges(visible, columns),
      })
    }
  }
  const visibleCount = visibleGroups.reduce((total, item) => total + item.visible.length, 0)
  const expandedGroups = expandedExpirations === undefined
    ? visibleGroups
    : visibleGroups.filter((item) => expandedExpirations.has(item.group.expiration))
  const expandedVisibleCount = expandedGroups.reduce((total, item) => total + item.visible.length, 0)
  let remaining = revealLimit
  const mountedGroups: VisibleGroup[] = []
  for (const item of expandedGroups) {
    if (remaining <= 0) break
    const visible = item.visible.slice(0, remaining)
    remaining -= visible.length
    mountedGroups.push({ ...item, visible })
  }
  const mountedCount = mountedGroups.reduce((total, item) => total + item.visible.length, 0)
  return {
    providerCount,
    visibleCount,
    visibleGroups,
    mountedGroups,
    mountedCount,
    expandedVisibleCount,
    remainingCount: Math.max(0, expandedVisibleCount - mountedCount),
    filterMiss: Boolean(page) && providerCount > 0 && visibleCount === 0,
    invertedDte,
  }
}
