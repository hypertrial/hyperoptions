import { Button } from "@/components/ui/button"
import { dateTime, moneyCents, percentTenths, unsignedPercentTenths } from "../format"
import { oddsAvailable, oddsProvenance, predictiveProvenance, preferredOddsKind } from "../marketOdds"
import OddsValues from "../OddsValues"
import type { WatchItem, WatchOutcome } from "./types"

function OddsSection({ item }: { item: WatchItem }) {
  const kind = preferredOddsKind(item.market_odds, item.predictive_odds)
  const provenance = kind === "market" ? oddsProvenance(item.market_odds) : predictiveProvenance(item.predictive_odds)
  const prior = item.last_available_market_odds
  return (
    <section className="watch-card-section" aria-label="Odds estimates">
      <h3>Expiry-close odds</h3>
      <p className="watch-muted">{kind === "predictive" ? "Historical stock-close forecast for the expiry trading session. It is distinct from risk-neutral option-implied odds." : kind === "market" ? "Risk-neutral estimate from option quotes for the regular-session close on expiry." : "No validated current estimate is available; see the reasons below."}</p>
      <p className="watch-odds"><OddsValues odds={item.market_odds} predictiveOdds={item.predictive_odds} /></p>
      {provenance ? <p className="watch-provenance">{provenance}</p> : null}
      {kind === "predictive" && item.market_odds?.reason ? <p className="watch-muted">Current market-implied odds unavailable: {item.market_odds.reason}</p> : null}
      {!oddsAvailable(item.market_odds) && prior && oddsAvailable(prior) ? (
        <details className="watch-prior-odds">
          <summary>Previous market-implied estimate · {prior.session_date ?? "dated quote"}</summary>
          <p>{unsignedPercentTenths(prior.itm_pct_tenths)} ITM · {unsignedPercentTenths(prior.otm_pct_tenths)} OTM</p>
          <p>{oddsProvenance(prior)}</p>
        </details>
      ) : null}
    </section>
  )
}

function HypotheticalRiskSection({ risk, side }: { risk: WatchItem["hypothetical_risk"]; side: WatchItem["side"] }) {
  const available = risk?.status === "available"
    && risk.assumed_spot_cents != null && risk.assumed_bid_cents != null
    && risk.expected_pnl_cents != null && risk.expected_return_pct_tenths != null
    && risk.loss_pct_tenths != null && risk.p05_pnl_cents != null && risk.quote_session != null
  return (
    <section className="watch-card-section watch-risk-section" aria-label="Hypothetical expiry risk">
      <h3>Hypothetical hold-to-expiry risk</h3>
      {available ? (
        <>
          <p className="watch-muted">One standard 100-share contract. Hypothetical hold-to-expiry; model returns adjusted to quote time. Excludes dividends, fees, assignment. No trade or position is recorded.</p>
          <dl className="watch-facts">
            <div><dt>{side === "call" ? "Assumed stock entry" : "Underlying quote used"}</dt><dd>{moneyCents(risk.assumed_spot_cents)} per share</dd></div>
            <div><dt>Assumed option bid</dt><dd>{moneyCents(risk.assumed_bid_cents)} per share</dd></div>
            <div><dt>Quote</dt><dd>{risk.quote_source === "nasdaq" ? "Nasdaq" : risk.quote_source === "yahoo" ? "Yahoo Finance" : "Public quote"} · session {risk.quote_session}</dd></div>
            <div><dt>Expected expiry P&amp;L</dt><dd>{moneyCents(risk.expected_pnl_cents)}</dd></div>
            <div><dt>Expected return</dt><dd>{percentTenths(risk.expected_return_pct_tenths)}</dd></div>
            <div><dt>Chance of loss</dt><dd>{unsignedPercentTenths(risk.loss_pct_tenths)}</dd></div>
            <div><dt>5th-percentile P&amp;L</dt><dd>{moneyCents(risk.p05_pnl_cents)}</dd></div>
          </dl>
        </>
      ) : <p className="watch-unavailable">Risk estimate unavailable. {risk?.reason ?? "A coherent entry quote and predictive distribution are required."}</p>}
    </section>
  )
}

function Outcome({ outcome, expiration }: { outcome: WatchOutcome; expiration: string }) {
  const provisional = outcome.status === "provisional" && outcome.classification != null
  const notExpired = outcome.status === "pending" && outcome.reason === "Expiry trading session has not completed"
  return (
    <section className="watch-card-section" aria-label="Expiry result">
      <h3>Expiry · close-based result</h3>
      {provisional ? (
        <>
          <p className="watch-outcome"><strong>Provisional {outcome.classification!.toUpperCase()}</strong> · indicative close-based result</p>
          {outcome.reason ? <p className="watch-unavailable" role="alert">Close recheck warning: {outcome.reason}</p> : null}
          {outcome.revised ? <p className="watch-muted">The sourced close was revised since the previous result.</p> : null}
          <dl className="watch-facts">
            <div><dt>Completed session</dt><dd>{outcome.session_date ?? "—"}</dd></div>
            <div><dt>Close</dt><dd>{outcome.close_exact == null ? "—" : `$${outcome.close_exact}`}</dd></div>
            <div><dt>Source</dt><dd>{outcome.source ?? "—"}</dd></div>
            <div><dt>Retrieved</dt><dd>{dateTime(outcome.retrieved_at)}</dd></div>
          </dl>
          <p className="watch-muted">{outcome.terms_note ?? "Assuming standard 100-share terms."} This is not an OCC exercise or assignment decision.</p>
        </>
      ) : notExpired ? (
        <p className="watch-unavailable"><strong>Not expired yet.</strong> The {outcome.session_date ?? expiration} expiry trading session has not completed. The close-based result will be checked afterward.</p>
      ) : (
        <p className="watch-unavailable">
          <strong>{outcome.status === "unsupported" ? "Expiry result unsupported." : "Expiry result pending."}</strong>{" "}
          {outcome.reason ?? "Waiting for a completed expiry-session close."}
        </p>
      )}
    </section>
  )
}

export default function WatchCard({ item, deleting, deleteError, onDelete }: {
  item: WatchItem
  deleting: boolean
  deleteError: string | null
  onDelete: (id: string) => void
}) {
  return (
    <article className="watch-card">
      <div className="watch-card-head">
        <div>
          <p className="eyebrow">{item.ticker}{item.root !== item.ticker ? ` · ${item.root} root` : ""}</p>
          <h2>{item.side === "call" ? "Call" : "Put"} · ${item.strike_exact} · {item.expiration}</h2>
          <p className="watch-muted">{item.terms_note || "Assuming standard 100-share terms."} Watched since {dateTime(item.created_at)}.</p>
        </div>
        <Button type="button" variant="outline" size="sm" disabled={deleting} onClick={() => onDelete(item.id)} aria-label={`Remove ${item.ticker} ${item.side} ${item.expiration} $${item.strike_exact} from watchlist`}>
          {deleting ? "Removing…" : "Remove"}
        </Button>
      </div>
      {deleteError ? <p role="alert" className="watch-error">{deleteError}</p> : null}
      <div className="watch-card-grid">
        <OddsSection item={item} />
        <Outcome outcome={item.outcome} expiration={item.expiration} />
        <HypotheticalRiskSection risk={item.hypothetical_risk} side={item.side} />
      </div>
    </article>
  )
}
