import { Button } from "@/components/ui/button"
import { dateTime } from "../format"
import { oddsProvenance, type MarketOdds } from "../marketOdds"
import OddsValues from "../OddsValues"
import type { WatchItem, WatchOutcome } from "./types"

function MarketOddsSection({ odds }: { odds: MarketOdds | null | undefined }) {
  const provenance = oddsProvenance(odds)
  return (
    <section className="watch-card-section" aria-label="Market-implied odds">
      <h3>Market-implied odds</h3>
      <p className="watch-muted">Risk-neutral estimate for the regular-session close on expiry.</p>
      <p className="watch-odds"><OddsValues odds={odds} /></p>
      {provenance ? <p className="watch-provenance">{provenance}</p> : null}
      {odds?.model_version ? <p className="watch-provenance">Model {odds.model_version}</p> : null}
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
          <p className="eyebrow">{item.ticker} · {item.root} root</p>
          <h2>{item.side === "call" ? "Call" : "Put"} · ${item.strike_exact} · {item.expiration}</h2>
          <p className="watch-muted">{item.terms_note || "Assuming standard 100-share terms."} Watched since {dateTime(item.created_at)}.</p>
        </div>
        <Button type="button" variant="outline" size="sm" disabled={deleting} onClick={() => onDelete(item.id)} aria-label={`Remove ${item.ticker} ${item.side} ${item.expiration} $${item.strike_exact} from watchlist`}>
          {deleting ? "Removing…" : "Remove"}
        </Button>
      </div>
      {deleteError ? <p role="alert" className="watch-error">{deleteError}</p> : null}
      <div className="watch-card-grid">
        <MarketOddsSection odds={item.market_odds} />
        <Outcome outcome={item.outcome} expiration={item.expiration} />
      </div>
    </article>
  )
}
