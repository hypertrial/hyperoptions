import { unsignedPercentTenths } from "./format"
import { oddsAvailable, oddsMessage, predictiveAvailable, predictiveBasisLabel, predictiveReliabilityLabel, quoteSupportLabel, type MarketOdds, type PredictiveOdds } from "./marketOdds"

export default function OddsValues({ odds, predictiveOdds, compact = false }: {
  odds: MarketOdds | null | undefined
  predictiveOdds?: PredictiveOdds | null
  compact?: boolean
}) {
  const predictive = predictiveAvailable(predictiveOdds)
  const market = oddsAvailable(odds)
  const reliability = predictive ? predictiveReliabilityLabel(predictiveOdds) : null
  const physicalStatus = predictiveOdds?.status === "pending" ? "Forecast pending" : "Forecast unavailable"
  return (
    <span className={compact ? "odds-values compact" : "odds-values"}>
      <span className="odds-physical">
        <span className="odds-method">Stock forecast</span>
        {predictive ? (
          <>
            <span className="odds-numbers">
              <span><strong>{unsignedPercentTenths(predictiveOdds!.itm_pct_tenths)}</strong> ITM</span>
              <span><strong>{unsignedPercentTenths(predictiveOdds!.otm_pct_tenths)}</strong> OTM</span>
            </span>
            <span className="odds-basis">{predictiveBasisLabel(predictiveOdds)}</span>
            {reliability !== "Reliability not yet established" || !compact ? <span className="odds-reliability">{reliability}</span> : null}
          </>
        ) : <span className="odds-unavailable" aria-label={`${physicalStatus}: ${predictiveOdds?.reason || "No validated forecast"}`}>{physicalStatus}{!compact && predictiveOdds?.reason ? ` · ${predictiveOdds.reason}` : ""}</span>}
      </span>
      <span className="odds-market">
        <span className="odds-method">Market odds · risk-neutral</span>
        {market ? (
          <>
            <span className="odds-market-numbers">{unsignedPercentTenths(odds!.itm_pct_tenths)} ITM · {unsignedPercentTenths(odds!.otm_pct_tenths)} OTM</span>
            {quoteSupportLabel(odds) ? <span className="odds-market-bounds" title="Quote-bound width measures option-price uncertainty, not forecast confidence.">{quoteSupportLabel(odds)}</span> : null}
          </>
        ) : <span className="odds-market-unavailable" title={oddsMessage(odds)}>{odds?.status === "pending" ? "Refreshing market odds" : `Market odds unavailable${!compact && odds?.reason ? `: ${odds.reason}` : ""}`}</span>}
      </span>
    </span>
  )
}
