import type { Side } from "./types"

export type FilterMetric = {
  key: string
  label: string
  scale: number
}

export type StrategySpec = {
  scaledFields: readonly string[]
  filterMetrics: {
    primary: FilterMetric
    apr: FilterMetric
    drop: FilterMetric
  }
  heatmapIds: readonly string[]
  defaultColumnIds: readonly string[]
  mobilePriorityIds: readonly string[]
}

export const STRATEGIES: Record<Side, StrategySpec> = {
  call: {
    scaledFields: ["stock_cost_cents", "premium_cents", "outlay_cents", "called_pnl_cents"],
    filterMetrics: {
      primary: { key: "called_pnl_cents", label: "Min Called P&L ($)", scale: 2 },
      apr: { key: "simple_apr_pct_tenths", label: "Min APR net (%)", scale: 1 },
      drop: { key: "drop_to_breakeven_pct_tenths", label: "Min Drop to breakeven (%)", scale: 1 },
    },
    heatmapIds: ["called_pnl_cents", "simple_apr_pct_tenths", "drop_to_breakeven_pct_tenths"],
    defaultColumnIds: [
      "strike_cents",
      "call_bid_cents",
      "call_spread_pct_tenths",
      "call_open_interest",
      "premium_cents",
      "called_pnl_cents",
      "simple_apr_pct_tenths",
      "drop_to_breakeven_pct_tenths",
    ],
    mobilePriorityIds: [
      "strike_cents",
      "call_bid_cents",
      "simple_apr_pct_tenths",
      "drop_to_breakeven_pct_tenths",
    ],
  },
  put: {
    scaledFields: ["premium_cents", "collateral_cents", "net_collateral_cents"],
    filterMetrics: {
      primary: { key: "premium_cents", label: "Min Premium ($)", scale: 2 },
      apr: { key: "apr_net_pct_tenths", label: "Min APR net (%)", scale: 1 },
      drop: { key: "cushion_to_breakeven_pct_tenths", label: "Min Cushion to breakeven (%)", scale: 1 },
    },
    heatmapIds: ["premium_cents", "apr_net_pct_tenths", "cushion_to_breakeven_pct_tenths"],
    defaultColumnIds: [
      "strike_cents",
      "put_bid_cents",
      "put_spread_pct_tenths",
      "put_open_interest",
      "premium_cents",
      "breakeven_cents",
      "apr_net_pct_tenths",
      "cushion_to_breakeven_pct_tenths",
    ],
    mobilePriorityIds: [
      "strike_cents",
      "put_bid_cents",
      "apr_net_pct_tenths",
      "cushion_to_breakeven_pct_tenths",
    ],
  },
}
