import type { CashSecuredPutContract, CoveredCallContract } from "./generated/types.gen"
import { integer, moneyCents, moneyStrike, percentTenths, signedE4, unsignedPercentTenths } from "./format"
import { STRATEGIES } from "./strategy"
import type { Side } from "./types"

export type SizedCall = CoveredCallContract
export type SizedPut = CashSecuredPutContract
export type SizedContract = SizedCall | SizedPut

export type ColumnId = string

export type ColumnGroup = "market" | "capital" | "returns" | "risk" | "history" | "greeks"

export type ColumnDef<T extends SizedContract = SizedContract> = {
  id: ColumnId
  label: string
  info: string
  abbrev?: boolean
  heatmap: boolean
  group: ColumnGroup
  greek?: boolean
  accessor: (row: T) => number | null | undefined
  format: (row: T) => string
}

export const CALL_DEFAULT_COLUMN_IDS = STRATEGIES.call.defaultColumnIds
export const PUT_DEFAULT_COLUMN_IDS = STRATEGIES.put.defaultColumnIds

const GREEK_INFO = {
  iv: "Black-Scholes implied volatility from the sell bid first, then mid. European no-dividend approximation. Shown as a percent.",
  delta: "Black-Scholes delta. Sensitivity of option price to a $1 move in the stock.",
  gamma: "Black-Scholes gamma. Sensitivity of delta to a $1 move in the stock.",
  theta: "Black-Scholes theta per share per calendar day.",
  vega: "Black-Scholes vega per 1 volatility point.",
  rho: "Black-Scholes rho per 1 percentage-point change in the risk-free rate.",
}


function field(side: Side, suffix: string): keyof SizedContract {
  return `${side}_${suffix}` as keyof SizedContract
}

function liquidityColumns(side: Side): ColumnDef[] {
  const name = side === "call" ? "Call" : "Put"
  return [
    {
      id: field(side, "ask_cents"),
      label: "Ask",
      info: `${name} ask. Shown with the bid so you can see the spread.`,
      heatmap: false,
      group: "market",
      accessor: (row) => row[field(side, "ask_cents")] as number | null,
      format: (row) => moneyCents(row[field(side, "ask_cents")] as number | null),
    },
    {
      id: field(side, "spread_cents"),
      label: "Spread",
      info: `${name} ask minus ${side} bid, in dollars.`,
      heatmap: false,
      group: "market",
      accessor: (row) => row[field(side, "spread_cents")] as number | null,
      format: (row) => moneyCents(row[field(side, "spread_cents")] as number | null),
    },
    {
      id: field(side, "spread_pct_tenths"),
      label: "Sprd %",
      info: "Bid-ask spread as a percent of the midpoint.",
      abbrev: true,
      heatmap: false,
      group: "market",
      accessor: (row) => row[field(side, "spread_pct_tenths")] as number | null,
      format: (row) => unsignedPercentTenths(row[field(side, "spread_pct_tenths")] as number | null),
    },
    {
      id: field(side, "volume"),
      label: "Vol",
      info: `${name} volume for this strike and expiration.`,
      abbrev: true,
      heatmap: false,
      group: "market",
      accessor: (row) => row[field(side, "volume")] as number | null,
      format: (row) => integer(row[field(side, "volume")] as number | null),
    },
    {
      id: field(side, "open_interest"),
      label: "OI",
      info: `${name} open interest. Missing OI or OI below 5 is omitted.`,
      abbrev: true,
      heatmap: false,
      group: "market",
      accessor: (row) => row[field(side, "open_interest")] as number | null,
      format: (row) => integer(row[field(side, "open_interest")] as number | null),
    },
  ]
}

const LOW_COLUMNS: ColumnDef[] = [
  {
    id: "vs_7d_low_pct_tenths",
    label: "7d",
    info: "(strike − 7-day completed low) / 7-day low. Positive means the strike is above that low.",
    abbrev: true,
    heatmap: false,
    group: "history",
    accessor: (row) => row.vs_7d_low_pct_tenths,
    format: (row) => percentTenths(row.vs_7d_low_pct_tenths),
  },
  {
    id: "vs_30d_low_pct_tenths",
    label: "30d",
    info: "(strike − 30-day completed low) / 30-day low. Positive means the strike is above that low.",
    abbrev: true,
    heatmap: false,
    group: "history",
    accessor: (row) => row.vs_30d_low_pct_tenths,
    format: (row) => percentTenths(row.vs_30d_low_pct_tenths),
  },
  {
    id: "vs_90d_low_pct_tenths",
    label: "90d",
    info: "(strike − 90-day completed low) / 90-day low. Positive means the strike is above that low.",
    abbrev: true,
    heatmap: false,
    group: "history",
    accessor: (row) => row.vs_90d_low_pct_tenths,
    format: (row) => percentTenths(row.vs_90d_low_pct_tenths),
  },
  {
    id: "vs_365d_low_pct_tenths",
    label: "365d",
    info: "(strike − 365-day completed low) / 365-day low. Positive means the strike is above that low.",
    abbrev: true,
    heatmap: false,
    group: "history",
    accessor: (row) => row.vs_365d_low_pct_tenths,
    format: (row) => percentTenths(row.vs_365d_low_pct_tenths),
  },
]

export const GREEK_COLUMNS: ColumnDef[] = [
  {
    id: "iv_pct_tenths",
    label: "IV",
    info: GREEK_INFO.iv,
    abbrev: true,
    heatmap: false,
    group: "greeks",
    greek: true,
    accessor: (row) => row.iv_pct_tenths,
    format: (row) => unsignedPercentTenths(row.iv_pct_tenths),
  },
  {
    id: "delta_e4",
    label: "Delta",
    info: GREEK_INFO.delta,
    heatmap: false,
    group: "greeks",
    greek: true,
    accessor: (row) => row.delta_e4,
    format: (row) => signedE4(row.delta_e4),
  },
  {
    id: "gamma_e4",
    label: "Gamma",
    info: GREEK_INFO.gamma,
    heatmap: false,
    group: "greeks",
    greek: true,
    accessor: (row) => row.gamma_e4,
    format: (row) => signedE4(row.gamma_e4),
  },
  {
    id: "theta_e4",
    label: "Theta",
    info: GREEK_INFO.theta,
    heatmap: false,
    group: "greeks",
    greek: true,
    accessor: (row) => row.theta_e4,
    format: (row) => signedE4(row.theta_e4),
  },
  {
    id: "vega_e4",
    label: "Vega",
    info: GREEK_INFO.vega,
    heatmap: false,
    group: "greeks",
    greek: true,
    accessor: (row) => row.vega_e4,
    format: (row) => signedE4(row.vega_e4),
  },
  {
    id: "rho_e4",
    label: "Rho",
    info: GREEK_INFO.rho,
    heatmap: false,
    group: "greeks",
    greek: true,
    accessor: (row) => row.rho_e4,
    format: (row) => signedE4(row.rho_e4),
  },
]

export const CALL_COLUMNS: ColumnDef<SizedCall>[] = [
  {
    id: "strike_cents",
    label: "Strike",
    info: "Call strike. Listed when open interest is at least 5 and the moneyness filter matches.",
    heatmap: false,
    group: "market",
    accessor: (row) => row.strike_cents,
    format: (row) => moneyStrike(row.strike_exact, row.strike_cents),
  },
  {
    id: "call_bid_cents",
    label: "Bid",
    info: "Call bid. Premium is 100 × bid. Net outlay is 100 × (current − bid). Effective cost uses current − bid.",
    heatmap: false,
    group: "market",
    accessor: (row) => row.call_bid_cents,
    format: (row) => moneyCents(row.call_bid_cents),
  },
  ...liquidityColumns("call"),

  {
    id: "stock_cost_cents",
    label: "Stock cost",
    info: "Gross stock cost: 100 × current. Contracts multiplies this.",
    heatmap: false,
    group: "capital",
    accessor: (row) => row.stock_cost_cents,
    format: (row) => moneyCents(row.stock_cost_cents),
  },
  {
    id: "premium_cents",
    label: "Premium",
    info: "Call premium received: 100 × call bid. Contracts multiplies this.",
    heatmap: false,
    group: "capital",
    accessor: (row) => row.premium_cents,
    format: (row) => moneyCents(row.premium_cents),
  },
  {
    id: "outlay_cents",
    label: "Net outlay",
    info: "Stock cost − premium = 100 × (current − call bid). Contracts multiplies this.",
    heatmap: false,
    group: "capital",
    accessor: (row) => row.outlay_cents,
    format: (row) => moneyCents(row.outlay_cents),
  },
  {
    id: "effective_cost_cents",
    label: "Effective cost",
    info: "Effective stock cost: current − call bid. Contracts does not scale this.",
    heatmap: false,
    group: "capital",
    accessor: (row) => row.effective_cost_cents,
    format: (row) => moneyCents(row.effective_cost_cents),
  },
  {
    id: "called_pnl_cents",
    label: "Called P&L",
    info: "Profit if assigned: premium − (current − strike) × 100, i.e. 100 × strike − net outlay. Contracts multiplies this.",
    heatmap: true,
    group: "returns",
    accessor: (row) => row.called_pnl_cents,
    format: (row) => moneyCents(row.called_pnl_cents),
  },
  {
    id: "called_pnl_per_share_cents",
    label: "P&L / sh",
    info: "Assigned profit per share: strike + call bid − current. Contracts does not scale this.",
    abbrev: true,
    heatmap: false,
    group: "returns",
    accessor: (row) => row.called_pnl_per_share_cents,
    format: (row) => moneyCents(row.called_pnl_per_share_cents),
  },
  {
    id: "simple_apr_pct_tenths",
    label: "APR (net)",
    info: "APR from one-contract net outlay: (called P&L / net outlay) × 365 / DTE.",
    abbrev: true,
    heatmap: true,
    group: "returns",
    accessor: (row) => row.simple_apr_pct_tenths,
    format: (row) => unsignedPercentTenths(row.simple_apr_pct_tenths),
  },
  {
    id: "stock_apr_pct_tenths",
    label: "APR (stock)",
    info: "APR from one-contract stock cost: (called P&L / gross stock cost) × 365 / DTE.",
    abbrev: true,
    heatmap: false,
    group: "returns",
    accessor: (row) => row.stock_apr_pct_tenths,
    format: (row) => unsignedPercentTenths(row.stock_apr_pct_tenths),
  },
  {
    id: "drop_to_strike_pct_tenths",
    label: "Drop (strike)",
    info: "Percent the stock would fall from current down to this strike.",
    abbrev: true,
    heatmap: false,
    group: "risk",
    accessor: (row) => row.drop_to_strike_pct_tenths,
    format: (row) => unsignedPercentTenths(row.drop_to_strike_pct_tenths),
  },
  {
    id: "drop_to_breakeven_pct_tenths",
    label: "Drop (BE)",
    info: "Percent the stock would fall from current down to effective cost (current − call bid). Contracts does not scale this.",
    abbrev: true,
    heatmap: true,
    group: "risk",
    accessor: (row) => row.drop_to_breakeven_pct_tenths,
    format: (row) => unsignedPercentTenths(row.drop_to_breakeven_pct_tenths),
  },
  ...LOW_COLUMNS,

]

export const PUT_COLUMNS: ColumnDef<SizedPut>[] = [
  {
    id: "strike_cents",
    label: "Strike",
    info: "Put strike. Listed when put open interest is at least 5 and the moneyness filter matches.",
    heatmap: false,
    group: "market",
    accessor: (row) => row.strike_cents,
    format: (row) => moneyStrike(row.strike_exact, row.strike_cents),
  },
  {
    id: "put_bid_cents",
    label: "Bid",
    info: "Put bid. Premium is 100 × bid. Collateral is 100 × strike.",
    heatmap: false,
    group: "market",
    accessor: (row) => row.put_bid_cents,
    format: (row) => moneyCents(row.put_bid_cents),
  },
  ...liquidityColumns("put"),

  {
    id: "premium_cents",
    label: "Premium",
    info: "Put premium received: 100 × put bid. Contracts multiplies this.",
    heatmap: true,
    group: "capital",
    accessor: (row) => row.premium_cents,
    format: (row) => moneyCents(row.premium_cents),
  },
  {
    id: "collateral_cents",
    label: "Collateral",
    info: "Cash required to secure the put: 100 × strike. Contracts multiplies this.",
    heatmap: false,
    group: "capital",
    accessor: (row) => row.collateral_cents,
    format: (row) => moneyCents(row.collateral_cents),
  },
  {
    id: "net_collateral_cents",
    label: "Net cash",
    info: "Collateral − premium. Contracts multiplies this.",
    heatmap: false,
    group: "capital",
    accessor: (row) => row.net_collateral_cents,
    format: (row) => moneyCents(row.net_collateral_cents),
  },
  {
    id: "breakeven_cents",
    label: "Breakeven",
    info: "Assigned breakeven: strike − put bid. Contracts does not scale this.",
    heatmap: false,
    group: "risk",
    accessor: (row) => row.breakeven_cents,
    format: (row) => moneyCents(row.breakeven_cents),
  },
  {
    id: "apr_collateral_pct_tenths",
    label: "APR (coll.)",
    info: "APR from one-contract collateral: (premium / collateral) × 365 / DTE.",
    abbrev: true,
    heatmap: false,
    group: "returns",
    accessor: (row) => row.apr_collateral_pct_tenths,
    format: (row) => unsignedPercentTenths(row.apr_collateral_pct_tenths),
  },
  {
    id: "apr_net_pct_tenths",
    label: "APR (net)",
    info: "APR from one-contract net cash: (premium / net collateral) × 365 / DTE.",
    abbrev: true,
    heatmap: true,
    group: "returns",
    accessor: (row) => row.apr_net_pct_tenths,
    format: (row) => unsignedPercentTenths(row.apr_net_pct_tenths),
  },
  {
    id: "cushion_to_strike_pct_tenths",
    label: "Cushion (K)",
    info: "Percent the stock can fall from current down to this strike.",
    abbrev: true,
    heatmap: false,
    group: "risk",
    accessor: (row) => row.cushion_to_strike_pct_tenths,
    format: (row) => unsignedPercentTenths(row.cushion_to_strike_pct_tenths),
  },
  {
    id: "cushion_to_breakeven_pct_tenths",
    label: "Cushion (BE)",
    info: "Percent the stock can fall from current down to breakeven (strike − put bid).",
    abbrev: true,
    heatmap: true,
    group: "risk",
    accessor: (row) => row.cushion_to_breakeven_pct_tenths,
    format: (row) => unsignedPercentTenths(row.cushion_to_breakeven_pct_tenths),
  },
  ...LOW_COLUMNS,

]

export function strategyColumns(side: Side): ColumnDef[] {
  const base = side === "put" ? PUT_COLUMNS : CALL_COLUMNS
  return [...base, ...GREEK_COLUMNS] as ColumnDef[]
}

export function defaultColumnIds(side: Side): string[] {
  return [...STRATEGIES[side].defaultColumnIds]
}

export function normalizeColumnIds(side: Side, selected: string[] | null): string[] | null {
  if (selected == null) return null
  const allowed = new Set(strategyColumns(side).map((column) => column.id))
  const normalized = [...new Set(selected.filter((id) => allowed.has(id)))]
  return normalized.length > 0 ? normalized : null
}

export function visibleColumns(side: Side, selected: string[] | null): ColumnDef[] {
  const all = strategyColumns(side)
  const chosen = normalizeColumnIds(side, selected) ?? defaultColumnIds(side)
  const byId = new Map(all.map((column) => [column.id, column]))
  return chosen.map((id) => byId.get(id)).filter((column): column is ColumnDef => column != null)
}

export function mobilePriorityColumns(columns: ColumnDef[], side: Side): ColumnDef[] {
  const preferred = [...STRATEGIES[side].mobilePriorityIds]
  const selected = new Map(columns.map((column) => [column.id, column]))
  const priority = preferred.map((id) => selected.get(id)).filter((column): column is ColumnDef => column != null)
  for (const column of columns) {
    if (priority.length >= 4) break
    if (!priority.some((item) => item.id === column.id)) priority.push(column)
  }
  return priority
}

export function formatContractValues(row: SizedContract, columns: ColumnDef[]): string[] {
  return columns.map((column) => column.format(row))
}
