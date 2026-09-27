import { unsignedPercentTenths } from "./format"
import { oddsAvailable, predictiveAvailable, predictiveSupportLabel, quoteSupportLabel, unavailableReasons, type MarketOdds, type PredictiveOdds } from "./marketOdds"

export default function OddsValues({ odds, predictiveOdds, compact = false }: {
  odds: MarketOdds | null | undefined
  predictiveOdds?: PredictiveOdds | null
  compact?: boolean
}) {
  const market = oddsAvailable(odds)
  const predictive = !market && predictiveAvailable(predictiveOdds)
  if (!market && !predictive) {
    const pending = odds?.status === "pending" || predictiveOdds?.status === "pending"
    const status = pending ? "Calculating odds…" : "Odds unavailable"
    return compact
      ? <span className="odds-unavailable compact" aria-label={pending ? status : `${status}: ${unavailableReasons(odds, predictiveOdds)}`}>{status}</span>
      : <span className="odds-unavailable"><strong>{status}</strong>{!pending ? <span>{unavailableReasons(odds, predictiveOdds)}</span> : null}</span>
  }
  const values = market ? odds! : predictiveOdds!
  const support = market ? quoteSupportLabel(odds) : compact ? predictiveSupportLabel(predictiveOdds) : null
  const supportTitle = market
    ? "Quote-bound tightness equals 100 minus the ITM bound width in percentage points. It is not forecast confidence."
    : predictiveOdds?.method === "empirical_scaled"
      ? "Model support counts overlapping matured horizon-return samples. Selection requires independent blocks. It is not forecast confidence."
      : "Model support counts daily returns for EWMA. It is not forecast confidence."
  return (
    <span className={compact ? "odds-values compact" : "odds-values"}>
      <span className="odds-method">{market ? "Market-implied" : "Historical predictive"}</span>
      <span><strong>{unsignedPercentTenths(values.itm_pct_tenths)}</strong> ITM</span>
      <span><strong>{unsignedPercentTenths(values.otm_pct_tenths)}</strong> OTM</span>
      {predictive ? <span><strong>{unsignedPercentTenths(predictiveOdds!.atm_pct_tenths)}</strong> ATM</span> : null}
      {support ? <span className="odds-support" title={supportTitle}>{support}</span> : null}
    </span>
  )
}
