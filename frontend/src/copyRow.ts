import { COPY_HEADERS } from "./columns"
import { integer, moneyCents } from "./format"

export type RowCopyContext = {
  ticker: string
  expiration: string
  dte: number
  currentSource: string
  currentCents: number | null | undefined
  contracts: number | null
}

export function copyRowAccessibleName(ticker: string, expiration: string, strikeDisplay: string): string {
  return `Copy row ${ticker} ${expiration} strike ${strikeDisplay}`
}

export function copyRowStateKey(ticker: string, expiration: string, strikeCents: number): string {
  return `${ticker}-${expiration}-${strikeCents}`
}

export function formatRowClipboard(
  context: RowCopyContext,
  headers: readonly string[] = COPY_HEADERS,
  values: readonly string[] = [],
): string {
  const parts = [
    context.ticker,
    context.expiration,
    `${integer(context.dte)} DTE`,
    `${context.currentSource}: ${moneyCents(context.currentCents)}`,
  ]
  if (context.contracts != null) {
    const lots = context.contracts === 1 ? "1 contract" : `${context.contracts.toLocaleString("en-US")} contracts`
    parts.push(`${lots} · ${(context.contracts * 100).toLocaleString("en-US")} sh`)
  }
  const headerRow = `| ${headers.join(" | ")} |`
  const divider = `| ${headers.map(() => "---").join(" | ")} |`
  const valueRow = `| ${values.join(" | ")} |`
  return `${parts.join(" · ")}\n\n${headerRow}\n${divider}\n${valueRow}`
}
