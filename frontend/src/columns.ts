import type { CashSecuredPutContract, CoveredCallContract } from "./generated/types.gen"
import { integer, moneyCents, moneyStrike, unsignedPercentTenths } from "./format"
import { STRATEGIES } from "./strategy"
import type { Side } from "./types"

export type SizedCall = CoveredCallContract
export type SizedPut = CashSecuredPutContract
export type SizedContract = SizedCall | SizedPut

export type ColumnId = string

export type ColumnDef<T extends SizedContract = SizedContract> = {
  id: ColumnId
  label: string
  info: string
  abbrev?: boolean
  heatmap: boolean
  accessor: (row: T) => number | null | undefined
  format: (row: T) => string
}

function quote(side: Side, row: SizedContract, field: "bid_cents" | "ask_cents" | "spread_pct_tenths" | "open_interest"): number | null {
  const key = `${side}_${field}` as keyof SizedContract
  const value = row[key]
  return typeof value === "number" ? value : null
}

export function chainColumns(side: Side): ColumnDef[] {
  const name = side === "call" ? "Call" : "Put"
  return [
    {
      id: "strike_cents",
      label: "Strike",
      info: `${name} strike. Listed when open interest is at least 5 and the moneyness filter matches.`,
      heatmap: false,
      accessor: (row) => row.strike_cents,
      format: (row) => moneyStrike(row.strike_exact, row.strike_cents),
    },
    {
      id: `${side}_bid_cents`,
      label: "Bid",
      info: `${name} bid. Premium (net) is 100 × (bid − intrinsic).`,
      heatmap: false,
      accessor: (row) => quote(side, row, "bid_cents"),
      format: (row) => moneyCents(quote(side, row, "bid_cents")),
    },
    {
      id: `${side}_ask_cents`,
      label: "Ask",
      info: `${name} ask. Shown with the bid so you can see the spread.`,
      heatmap: false,
      accessor: (row) => quote(side, row, "ask_cents"),
      format: (row) => moneyCents(quote(side, row, "ask_cents")),
    },
    {
      id: `${side}_spread_pct_tenths`,
      label: "Spread (%)",
      info: "Bid-ask spread as a percent of the midpoint.",
      abbrev: true,
      heatmap: false,
      accessor: (row) => quote(side, row, "spread_pct_tenths"),
      format: (row) => unsignedPercentTenths(quote(side, row, "spread_pct_tenths")),
    },
    {
      id: "iv_pct_tenths",
      label: "IV",
      info: "European no-dividend Black-Scholes implied volatility from the bid/ask midpoint. Open details for bid/ask IV, calculation inputs, or an unavailable reason. Contracts does not scale this.",
      abbrev: true,
      heatmap: false,
      accessor: (row) => row.iv_pct_tenths,
      format: (row) => unsignedPercentTenths(row.iv_pct_tenths),
    },
    {
      id: `${side}_open_interest`,
      label: "OI",
      info: `${name} open interest for this strike and expiration.`,
      abbrev: true,
      heatmap: false,
      accessor: (row) => quote(side, row, "open_interest"),
      format: (row) => integer(quote(side, row, "open_interest")),
    },
    {
      id: "net_premium_cents",
      label: "Premium (net)",
      info: "Time value received: 100 × (bid − intrinsic). Intrinsic is max(0, current − strike) for calls and max(0, strike − current) for puts. Contracts multiplies this.",
      heatmap: true,
      accessor: (row) => row.net_premium_cents,
      format: (row) => moneyCents(row.net_premium_cents),
    },
    {
      id: "net_apr_pct_tenths",
      label: "APR (net)",
      info: side === "call"
        ? "APR from one-contract net capital: (net premium / (100 × (current − bid))) × 365 / DTE."
        : "APR from one-contract net capital: (net premium / (100 × (strike − bid))) × 365 / DTE.",
      abbrev: true,
      heatmap: true,
      accessor: (row) => row.net_apr_pct_tenths,
      format: (row) => unsignedPercentTenths(row.net_apr_pct_tenths),
    },
    {
      id: "breakeven_cents",
      label: "Breakeven",
      info: side === "call"
        ? "Breakeven stock price: current − call bid. Contracts does not scale this."
        : "Assigned breakeven: strike − put bid. Contracts does not scale this.",
      heatmap: false,
      accessor: (row) => row.breakeven_cents,
      format: (row) => moneyCents(row.breakeven_cents),
    },
    {
      id: "breakeven_change_pct_tenths",
      label: "% to assignment",
      info: side === "call"
        ? "Percent the stock must rise from the current price to the strike before assignment. Negative when the stock is already above the strike. Contracts does not scale this."
        : "Percent the stock must fall from the current price to the strike before assignment. Negative when the stock is already below the strike. Contracts does not scale this.",
      abbrev: true,
      heatmap: true,
      accessor: (row) => row.breakeven_change_pct_tenths,
      format: (row) => unsignedPercentTenths(row.breakeven_change_pct_tenths),
    },
  ]
}

export function visibleColumns(side: Side): ColumnDef[] {
  return chainColumns(side)
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
