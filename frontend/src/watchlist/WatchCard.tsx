import { Button } from "@/components/ui/button"
import { dateTime } from "../format"
import type { WatchForecast, WatchItem, WatchOutcome } from "./types"

const unavailableReasons: Record<string, string> = {
  model_not_ready: "The pooled model is still being prepared.",
  cohort_insufficient: "The peer cohort did not meet the sample requirements.",
  validation_failed: "The model did not pass its holdout validation.",
  ticker_history_short: "This ticker has too little completed price history.",
  ticker_strategy_unqualified: "No strategy passed this ticker’s validation gate.",
  market_data_missing: "Completed market data is missing.",
  stale_model: "The validated model is too old.",
  horizon_unsupported: "This expiration horizon has insufficient audited evidence.",
  strike_outside_support: "This strike is outside the audited return range.",
  audit_window_incomplete: "The 2023–2025 holdout period has not completed.",
  ticker_in_calibration_cohort: "This ticker belongs to the frozen peer cohort, so an independent forecast is unavailable.",
  cohort_engine_incompatible: "The forecast engine changed after the peer cohort was frozen.",
  cohort_prefix_revised: "A frozen peer’s selection history was revised.",
  peer_data_missing: "Peer price history is temporarily unavailable.",
  peer_selection_failed: "A frozen peer’s strategy no longer passes selection.",
  expiry_completed: "This contract’s expiry session has completed.",
}

function reasonText(reason: string | null | undefined, fallback: string): string {
  if (!reason) return fallback
  return unavailableReasons[reason] ?? (/^[a-z_]+$/.test(reason) ? reason.replaceAll("_", " ") : reason)
}

function ForecastEvidence({ forecast, historical = false }: { forecast: WatchForecast | null; historical?: boolean }) {
  const available = forecast?.status === "available" && forecast.itm_probability != null && Number.isFinite(forecast.itm_probability)
  return (
    <>
      {available ? (
        <>
          <p className="watch-probability"><strong>{(forecast.itm_probability! * 100).toFixed(1)}%</strong> ITM probability</p>
          <p className="watch-muted">As of completed market session {forecast.as_of ?? "unavailable"}. {historical ? "This stored pre-expiry estimate is historical, not a current probability or expiry result." : "This is a model estimate, not an expiry result."}</p>
          <dl className="watch-facts">
            <div><dt>Strategy</dt><dd>{forecast.strategy_name ?? forecast.strategy_id ?? "—"}</dd></div>
            <div><dt>Signal</dt><dd>{forecast.signal_state ?? "—"}</dd></div>
            <div><dt>Frozen cohort</dt><dd>{forecast.cohort_size?.toLocaleString("en-US") ?? "—"} peers</dd></div>
            <div><dt>Fitting peers</dt><dd>{forecast.fit_peers?.toLocaleString("en-US") ?? "—"}</dd></div>
            <div><dt>Fitting / audit observations</dt><dd>{forecast.fit_samples?.toLocaleString("en-US") ?? "—"} / {forecast.audit_samples?.toLocaleString("en-US") ?? "—"}</dd></div>
            <div><dt>Audit peers / blocks</dt><dd>{forecast.audit_peers ?? "—"} / {forecast.audit_blocks ?? "—"}</dd></div>
            <div><dt>Holdout model-skill uncertainty</dt><dd>{forecast.crps_skill_lower_90 == null ? "—" : `${forecast.crps_skill_lower_90.toFixed(3)} CRPS improvement lower 90% bound`}</dd></div>
            <div><dt>Holdout Brier change</dt><dd>{forecast.brier_delta == null ? "—" : forecast.brier_delta.toFixed(4)}</dd></div>
          </dl>
          {forecast.model_id ? <p className="watch-provenance">Model version {forecast.model_id}</p> : null}
          {forecast.source ? <p className="watch-provenance">Forecast price source: {forecast.source}</p> : null}
          {forecast.survivorship_note ? <p className="watch-limitation">{forecast.survivorship_note}</p> : null}
        </>
      ) : (
        <p className="watch-unavailable"><strong>Probability unavailable.</strong> {reasonText(forecast?.reason, "Forecast preparation pending.")}</p>
      )}
    </>
  )
}

function Forecast({ forecast, lastAvailable, expired }: { forecast: WatchForecast | null; lastAvailable: WatchForecast | null; expired: boolean }) {
  const historical = forecast?.historical === true || (expired && forecast?.status === "available")
  return (
    <section className="watch-card-section" aria-label="Pre-expiry forecast">
      <h3>{historical ? "Historical pre-expiry forecast" : "Pre-expiry · experimental forecast"}</h3>
      <ForecastEvidence forecast={forecast} historical={historical} />
      {forecast?.status !== "available" && lastAvailable?.status === "available" ? (
        <div className="watch-historical">
          <h4>Historical pre-expiry forecast</h4>
          <ForecastEvidence forecast={lastAvailable} historical />
        </div>
      ) : null}
    </section>
  )
}

function Outcome({ outcome }: { outcome: WatchOutcome }) {
  const provisional = outcome.status === "provisional" && outcome.classification != null
  return (
    <section className="watch-card-section" aria-label="Expiry result">
      <h3>Expiry · close-based result</h3>
      {provisional ? (
        <>
          <p className="watch-outcome"><strong>Provisional {outcome.classification!.toUpperCase()}</strong> · indicative close-based result</p>
          {outcome.reason ? <p className="watch-unavailable" role="alert">Close recheck warning: {reasonText(outcome.reason, outcome.reason)}</p> : null}
          {outcome.revised ? <p className="watch-muted">The sourced close was revised since the previous result.</p> : null}
          <dl className="watch-facts">
            <div><dt>Completed session</dt><dd>{outcome.session_date ?? "—"}</dd></div>
            <div><dt>Close</dt><dd>{outcome.close_exact == null ? "—" : `$${outcome.close_exact}`}</dd></div>
            <div><dt>Source</dt><dd>{outcome.source ?? "—"}</dd></div>
            <div><dt>Retrieved</dt><dd>{dateTime(outcome.retrieved_at)}</dd></div>
          </dl>
          <p className="watch-muted">{outcome.terms_note ?? "Assuming standard 100-share terms."} This is not an OCC exercise or assignment decision.</p>
        </>
      ) : (
        <p className="watch-unavailable">
          <strong>{outcome.status === "unsupported" ? "Expiry result unsupported." : "Expiry result pending."}</strong>{" "}
          {reasonText(outcome.reason, "Waiting for a completed expiry-session close.")}
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
  const expirySessionChecked = item.outcome.session_date != null
    && item.outcome.reason !== "Expiry trading session has not completed"
  const expired = item.outcome.status !== "pending"
    || expirySessionChecked
    || new Date().toISOString().slice(0, 10) > item.expiration
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
        <Forecast forecast={item.forecast} lastAvailable={item.last_available_forecast ?? null} expired={expired} />
        <Outcome outcome={item.outcome} />
      </div>
    </article>
  )
}
