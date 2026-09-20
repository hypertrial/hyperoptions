import { moneyCents } from "./format"
import type { ChainPage } from "./types"

const SHARES_PER_CONTRACT = 100

export function parseContractCount(raw: string): number | null {
  const text = raw.replace(/[,\s]/g, "")
  if (text === "") return null
  if (!/^\d+$/.test(text)) return null
  const count = Number(text)
  if (!Number.isSafeInteger(count) || count < 1) return null
  return count
}

export function scaleByContracts(
  value: number | null | undefined,
  contracts: number,
): number | null {
  if (value == null || !Number.isSafeInteger(value) || !Number.isSafeInteger(contracts) || contracts <= 0) {
    return null
  }
  const scaled = value * contracts
  return Number.isSafeInteger(scaled) ? scaled : null
}

export function shareCount(contracts: number): number | null {
  if (!Number.isSafeInteger(contracts) || contracts <= 0) return null
  const shares = contracts * SHARES_PER_CONTRACT
  return Number.isSafeInteger(shares) ? shares : null
}

export function stockCapitalCents(
  contracts: number,
  currentCents: number | null | undefined,
): number | null {
  if (!Number.isInteger(contracts) || contracts <= 0) return null
  if (currentCents == null || !Number.isSafeInteger(currentCents)) {
    return null
  }
  const shares = shareCount(contracts)
  if (shares == null) return null
  const capital = shares * currentCents
  return Number.isSafeInteger(capital) ? capital : null
}

const CALL_SCALED_FIELDS = ["stock_cost_cents", "premium_cents", "outlay_cents", "called_pnl_cents"] as const
const PUT_SCALED_FIELDS = ["premium_cents", "collateral_cents", "net_collateral_cents"] as const

export function contractCountIsSafe(contracts: number, page: ChainPage | null): boolean {
  if (shareCount(contracts) == null) return false
  if (page?.current_cents != null && stockCapitalCents(contracts, page.current_cents) == null) return false
  for (const group of page?.expirations ?? []) {
    for (const row of group.contracts) {
      const record = row as unknown as Record<string, number | null>
      const fields = "stock_cost_cents" in record ? CALL_SCALED_FIELDS : PUT_SCALED_FIELDS
      for (const field of fields) {
        const value = record[field]
        if (value != null && scaleByContracts(value, contracts) == null) return false
      }
    }
  }
  return true
}

export function contractSizeLabel(
  contracts: number,
  currentCents: number | null | undefined,
): string {
  const lots = contracts === 1 ? "1 contract" : `${contracts.toLocaleString("en-US")} contracts`
  const shares = shareCount(contracts)
  const parts = [lots]
  if (shares != null) parts.push(`${shares.toLocaleString("en-US")} sh`)
  const stock = stockCapitalCents(contracts, currentCents)
  if (stock != null) parts.push(`${moneyCents(stock)} stock`)
  return parts.join(" · ")
}
