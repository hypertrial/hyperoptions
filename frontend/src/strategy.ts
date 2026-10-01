import type { Side } from "./types"

export type FilterMetric = {
  key: string
  label: string
  scale: number
}

export type StrategySpec = {
  scaledFields: readonly string[]
  filterMetrics: {
    premium: FilterMetric
    apr: FilterMetric
    breakeven: FilterMetric
  }
  mobilePriorityIds: readonly string[]
}

const FILTER_METRICS: StrategySpec["filterMetrics"] = {
  premium: { key: "net_premium_cents", label: "Min Premium (net) ($)", scale: 2 },
  apr: { key: "net_apr_pct_tenths", label: "Min APR (net) (%)", scale: 1 },
  breakeven: { key: "breakeven_change_pct_tenths", label: "Min % to assignment (%)", scale: 1 },
}

export const STRATEGIES: Record<Side, StrategySpec> = {
  call: {
    scaledFields: ["stock_cost_cents", "premium_cents", "outlay_cents", "called_pnl_cents", "net_premium_cents"],
    filterMetrics: FILTER_METRICS,
    mobilePriorityIds: [
      "strike_cents",
      "call_bid_cents",
      "net_apr_pct_tenths",
      "breakeven_change_pct_tenths",
    ],
  },
  put: {
    scaledFields: ["premium_cents", "collateral_cents", "net_collateral_cents", "net_premium_cents"],
    filterMetrics: FILTER_METRICS,
    mobilePriorityIds: [
      "strike_cents",
      "put_bid_cents",
      "net_apr_pct_tenths",
      "breakeven_change_pct_tenths",
    ],
  },
}
