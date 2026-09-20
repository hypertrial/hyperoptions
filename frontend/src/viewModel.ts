import type { ColumnDef, ColumnId, SizedContract } from "./columns"
import { scaleByContracts } from "./contracts"
import { invertedDteRange, passesFilters, type FilterState } from "./filters"
import { metricRanges, type MetricRanges } from "./heatmap"
import type { ChainPage, Side } from "./types"

export type SortState = { id: ColumnId; dir: "asc" | "desc" }

export const DEFAULT_SORT: SortState = { id: "strike_cents", dir: "desc" }

export type { FilterState } from "./filters"
export { invertedDteRange } from "./filters"

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
  sizedContracts: number
  visibleGroups: VisibleGroup[]
  mountedGroups: VisibleGroup[]
  mountedCount: number
  expandedVisibleCount: number
  remainingCount: number
  filterMiss: boolean
  invertedDte: boolean
}

function scaleRow(row: SizedContract, contracts: number, side: Side): SizedContract {
  if (side === "put") {
    const put = row as SizedContract & {
      premium_cents: number | null
      collateral_cents: number | null
      net_collateral_cents: number | null
    }
    return {
      ...put,
      premium_cents: scaleByContracts(put.premium_cents, contracts),
      collateral_cents: scaleByContracts(put.collateral_cents, contracts),
      net_collateral_cents: scaleByContracts(put.net_collateral_cents, contracts),
    }
  }
  const call = row as SizedContract & {
    stock_cost_cents: number | null
    premium_cents: number | null
    outlay_cents: number | null
    called_pnl_cents: number | null
  }
  return {
    ...call,
    stock_cost_cents: scaleByContracts(call.stock_cost_cents, contracts),
    premium_cents: scaleByContracts(call.premium_cents, contracts),
    outlay_cents: scaleByContracts(call.outlay_cents, contracts),
    called_pnl_cents: scaleByContracts(call.called_pnl_cents, contracts),
  }
}

export function nearestMatchingExpiration(
  page: ChainPage | null,
  contracts: number,
  filters: FilterState,
  side: Side,
): string | null {
  let nearest: ChainPage["expirations"][number] | null = null
  for (const group of page?.expirations ?? []) {
    const matches = group.contracts.some((row) => (
      passesFilters(
        scaleRow(row as SizedContract, contracts, side) as unknown as Record<string, number | null>,
        filters,
        side,
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
  const sizedContracts = contracts
  const providerCount = page?.expirations.reduce((total, group) => total + group.contracts.length, 0) ?? 0
  const visibleGroups: VisibleGroup[] = []
  if (page) {
    for (const group of page.expirations) {
      const visible: SizedContract[] = []
      for (const row of group.contracts) {
        const sized = scaleRow(row as SizedContract, sizedContracts, side)
        if (passesFilters(sized as unknown as Record<string, number | null>, filters, side)) {
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
    sizedContracts,
    visibleGroups,
    mountedGroups,
    mountedCount,
    expandedVisibleCount,
    remainingCount: Math.max(0, expandedVisibleCount - mountedCount),
    filterMiss: Boolean(page) && providerCount > 0 && visibleCount === 0,
    invertedDte,
  }
}
