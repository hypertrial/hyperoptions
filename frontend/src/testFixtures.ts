import { visibleColumns } from "./columns"
import { STRATEGIES } from "./strategy"
import type { CashSecuredPutContract, CashSecuredPutPage, CoveredCallContract, CoveredCallPage } from "./types"

export const COLUMN_HEADERS = visibleColumns("call", null)
export const COPY_HEADERS = COLUMN_HEADERS.map((column) => column.label)
export const METRIC_KEYS = [...STRATEGIES.call.heatmapIds]

export function sampleContract(overrides: Partial<CoveredCallContract> = {}): CoveredCallContract {
  return {
    expiration: "2026-09-18",
    dte: 7,
    strike_cents: 5000,
    in_the_money: true,
    call_bid_cents: 50,
    call_ask_cents: 51,
    call_spread_cents: 1,
    call_spread_pct_tenths: 20,
    call_volume: 12,
    call_open_interest: 55,
    stock_cost_cents: 499_000,
    premium_cents: 5000,
    outlay_cents: 496_000,
    effective_cost_cents: 4940,
    called_pnl_cents: 4000,
    called_pnl_per_share_cents: 60,
    simple_apr_pct_tenths: 421,
    stock_apr_pct_tenths: 418,
    drop_to_strike_pct_tenths: 2,
    drop_to_breakeven_pct_tenths: 10,
    vs_7d_low_pct_tenths: 250,
    vs_30d_low_pct_tenths: 429,
    vs_90d_low_pct_tenths: 667,
    vs_365d_low_pct_tenths: 1500,
    iv_pct_tenths: 450,
    delta_e4: 6368,
    gamma_e4: 188,
    theta_e4: -176,
    vega_e4: 3752,
    rho_e4: 5323,
    greeks_source: "bid",
    ...overrides,
  }
}

export function missingContract(overrides: Partial<CoveredCallContract> = {}): CoveredCallContract {
  return sampleContract({
    strike_cents: 4050,
    call_bid_cents: null,
    call_ask_cents: null,
    call_spread_cents: null,
    call_spread_pct_tenths: null,
    call_volume: null,
    call_open_interest: null,
    stock_cost_cents: 499_000,
    premium_cents: null,
    outlay_cents: null,
    effective_cost_cents: null,
    called_pnl_cents: null,
    called_pnl_per_share_cents: null,
    simple_apr_pct_tenths: null,
    stock_apr_pct_tenths: null,
    drop_to_strike_pct_tenths: 192,
    drop_to_breakeven_pct_tenths: null,
    vs_7d_low_pct_tenths: 13,
    vs_30d_low_pct_tenths: 157,
    vs_90d_low_pct_tenths: 350,
    vs_365d_low_pct_tenths: 1025,
    iv_pct_tenths: null,
    delta_e4: null,
    gamma_e4: null,
    theta_e4: null,
    vega_e4: null,
    rho_e4: null,
    greeks_source: null,
    ...overrides,
  })
}

export function laterContract(overrides: Partial<CoveredCallContract> = {}): CoveredCallContract {
  return sampleContract({
    expiration: "2026-10-09",
    dte: 28,
    strike_cents: 4500,
    call_bid_cents: 800,
    call_ask_cents: 820,
    call_spread_cents: 20,
    call_spread_pct_tenths: 25,
    call_volume: 4,
    call_open_interest: 20,
    stock_cost_cents: 499_000,
    premium_cents: 80_000,
    outlay_cents: 421_000,
    effective_cost_cents: 4200,
    called_pnl_cents: 29_000,
    called_pnl_per_share_cents: 310,
    simple_apr_pct_tenths: 898,
    stock_apr_pct_tenths: 758,
    drop_to_strike_pct_tenths: 102,
    drop_to_breakeven_pct_tenths: 160,
    vs_7d_low_pct_tenths: 125,
    vs_30d_low_pct_tenths: 286,
    vs_90d_low_pct_tenths: 500,
    vs_365d_low_pct_tenths: 1250,
    ...overrides,
  })
}

export function samplePage(overrides: Partial<CoveredCallPage> = {}): CoveredCallPage {
  return {
    ticker: "IREN",
    name: "Iris Energy Limited",
    options_available: true,
    moneyness: "itm",
    fetched_at: "2026-09-11T14:00:00Z",
    current_cents: 4990,
    current_source: "stock_bid",
    stock_bid_cents: 4990,
    stock_ask_cents: 5010,
    market_session: "Market",
    is_real_time: true,
    quote_timestamp: "Sep 11, 2026 10:00 AM ET",
    last_trade: "LAST TRADE: $43.93 (AS OF SEP 10, 2026 3:37 PM ET)",
    last_trade_timestamp: "SEP 10, 2026 3:37 PM ET",
    truncated: false,
    chain_from_cache: false,
    info_from_cache: false,
    history_from_cache: false,
    risk_free_rate_pct_tenths: 40,
    lows: { d7_cents: 4000, d30_cents: 3500, d90_cents: 3000, d365_cents: 2000 },
    expirations: [
      {
        expiration: "2026-09-18",
        dte: 7,
        contracts: [sampleContract(), missingContract()],
      },
      {
        expiration: "2026-10-09",
        dte: 28,
        contracts: [laterContract()],
      },
    ],
    ...overrides,
  }
}

export function samplePutContract(overrides: Partial<CashSecuredPutContract> = {}): CashSecuredPutContract {
  return {
    expiration: "2026-09-18",
    dte: 7,
    strike_cents: 4500,
    in_the_money: false,
    put_bid_cents: 80,
    put_ask_cents: 90,
    put_spread_cents: 10,
    put_spread_pct_tenths: 118,
    put_volume: 20,
    put_open_interest: 40,
    premium_cents: 8000,
    collateral_cents: 450_000,
    net_collateral_cents: 442_000,
    breakeven_cents: 4420,
    apr_collateral_pct_tenths: 928,
    apr_net_pct_tenths: 945,
    cushion_to_strike_pct_tenths: 98,
    cushion_to_breakeven_pct_tenths: 114,
    vs_7d_low_pct_tenths: 125,
    vs_30d_low_pct_tenths: 286,
    vs_90d_low_pct_tenths: 500,
    vs_365d_low_pct_tenths: 1250,
    iv_pct_tenths: 380,
    delta_e4: -3632,
    gamma_e4: 188,
    theta_e4: -176,
    vega_e4: 3752,
    rho_e4: -5323,
    greeks_source: "bid",
    ...overrides,
  }
}

export function samplePutPage(overrides: Partial<CashSecuredPutPage> = {}): CashSecuredPutPage {
  return {
    ticker: "IREN",
    name: "Iris Energy Limited",
    options_available: true,
    moneyness: "otm",
    fetched_at: "2026-09-11T14:00:00Z",
    current_cents: 4990,
    current_source: "stock_bid",
    stock_bid_cents: 4990,
    stock_ask_cents: 5010,
    market_session: "Market",
    is_real_time: true,
    quote_timestamp: "Sep 11, 2026 10:00 AM ET",
    last_trade: "LAST TRADE: $43.93 (AS OF SEP 10, 2026 3:37 PM ET)",
    last_trade_timestamp: "SEP 10, 2026 3:37 PM ET",
    truncated: false,
    chain_from_cache: false,
    info_from_cache: false,
    history_from_cache: false,
    risk_free_rate_pct_tenths: 40,
    lows: { d7_cents: 4000, d30_cents: 3500, d90_cents: 3000, d365_cents: 2000 },
    expirations: [
      {
        expiration: "2026-09-18",
        dte: 7,
        contracts: [samplePutContract()],
      },
    ],
    ...overrides,
  }
}

export function largeChainPage(rowCount: number): CoveredCallPage {
  const contracts: CoveredCallContract[] = []
  for (let index = 0; index < rowCount; index += 1) {
    const strike = 4990 - (index + 1)
    contracts.push(sampleContract({
      expiration: "2026-09-18",
      strike_cents: strike,
      outlay_cents: 1000 + index,
      called_pnl_cents: 100 + index,
    }))
  }
  return samplePage({
    expirations: [{ expiration: "2026-09-18", dte: 7, contracts }],
  })
}
