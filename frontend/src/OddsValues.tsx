import { unsignedPercentTenths } from "./format"
import { oddsAvailable, oddsMessage, type MarketOdds } from "./marketOdds"

export default function OddsValues({ odds, compact = false }: { odds: MarketOdds | null | undefined; compact?: boolean }) {
  if (!oddsAvailable(odds)) {
    return <span className={compact ? "odds-unavailable compact" : "watch-unavailable"}>{oddsMessage(odds)}</span>
  }
  return (
    <span className={compact ? "odds-values compact" : "odds-values"}>
      <span><strong>{unsignedPercentTenths(odds!.itm_pct_tenths)}</strong> ITM</span>
      <span><strong>{unsignedPercentTenths(odds!.otm_pct_tenths)}</strong> OTM</span>
    </span>
  )
}
