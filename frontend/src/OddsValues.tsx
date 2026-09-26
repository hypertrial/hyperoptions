import { unsignedPercentTenths } from "./format"
import { oddsAvailable, oddsMessage, type MarketOdds } from "./marketOdds"

export default function OddsValues({ odds, compact = false }: { odds: MarketOdds | null | undefined; compact?: boolean }) {
  if (!oddsAvailable(odds)) {
    const pending = odds?.status === "pending"
    const status = pending ? "Calculating odds…" : "Odds unavailable"
    return compact
      ? <span className="odds-unavailable compact" aria-label={pending ? status : `${status}: ${oddsMessage(odds)}`}>{status}</span>
      : <span className="odds-unavailable"><strong>{status}</strong>{!pending ? <span>{oddsMessage(odds)}</span> : null}</span>
  }
  return (
    <span className={compact ? "odds-values compact" : "odds-values"}>
      <span><strong>{unsignedPercentTenths(odds!.itm_pct_tenths)}</strong> ITM</span>
      <span><strong>{unsignedPercentTenths(odds!.otm_pct_tenths)}</strong> OTM</span>
    </span>
  )
}
