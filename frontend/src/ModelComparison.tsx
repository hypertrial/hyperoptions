import { useState } from "react"
import type { MarketOddsView, PredictiveOddsView } from "./generated/types.gen"
import { marketModelName, physicalModelName, type PhysicalModel } from "./forecastModels"
import { unsignedPercentTenths } from "./format"
import { oddsAvailable, predictiveAvailable, quoteSupportLabel } from "./marketOdds"

type Data = Record<string, unknown>

function data(value: unknown): Data | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Data : null
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

function count(value: unknown): string {
  const n = number(value)
  return n == null ? "0" : n.toLocaleString("en-US")
}

function score(value: unknown): string {
  const n = number(value)
  return n == null ? "N/A" : n.toFixed(3)
}

function milliseconds(value: unknown): string {
  const n = number(value)
  return n == null ? "N/A" : `${n.toFixed(0)} ms`
}

function percent(value: unknown): string {
  const n = number(value)
  return n == null ? "N/A" : `${(n * 100).toFixed(1)}%`
}

function reasonList(value: unknown): string {
  const reasons = data(value)
  return reasons && Object.keys(reasons).length
    ? Object.entries(reasons).map(([name, amount]) => `${name}: ${count(amount)}`).join(" · ")
    : "None recorded"
}

function evidencePanel(title: string, value: unknown) {
  const report = data(value)
  if (!report) return <p>{title}: no report available. N=0 · Brier N/A · significance not estimable.</p>
  const brier = data(report.brier)
  const logLoss = data(report.log_loss)
  const blocks = number(report.independent_date_blocks) ?? 0
  const units = number(report.ticker_origin_horizon_units) ?? 0
  const attempted = number(report.contract_cells_attempted) ?? 0
  const available = number(report.contract_forecasts_available) ?? 0
  const interval = brier?.bootstrap_95
  const intervalText = report.significance === "not_applicable" ? "not applicable for the baseline"
    : blocks >= 20 && Array.isArray(interval) && interval.length === 2
      ? `[${score(interval[0])}, ${score(interval[1])}]` : "significance not estimable"
  const calibration = data(report.calibration_by_side)
  const latency = data(report.latency_ms)
  return <section className="model-evidence" aria-label={`${title} evidence`}>
    <h5>{title}</h5>
    <p>{title === "Retrospective replay" ? "Current-vintage screening; not an as-issued accuracy claim." : "Prospective as-issued accuracy; descriptive until enough outcomes mature."}</p>
    <p>Report {typeof report.generated_at === "string" ? report.generated_at.slice(0, 10) : "date unavailable"} · model {String(report.model_version ?? "unknown")} · input {String(report.input_version ?? "unknown")} · report hash {typeof report.report_hash === "string" ? report.report_hash.slice(0, 12) : "unrecorded"}</p>
    {typeof report.audit_session === "string" ? <p>Frozen audit cohort: {report.audit_session}{typeof report.audit_frozen_at === "string" ? ` · captured ${report.audit_frozen_at.slice(0, 10)}` : ""}</p> : null}
    <p>{count(report.tickers)} tickers · {count(blocks)} independent date blocks · N={count(units)} ticker-origin-horizon units</p>
    <p>Coverage {count(available)}/{count(attempted)} recorded contract cells{attempted > 0 ? ` (${percent(available / attempted)})` : " (N/A)"} · rejections: {reasonList(report.rejection_reasons)}</p>
    {report.coverage_basis === "recorded_contract_cells_with_baseline_issuance" ? <p>This coverage is conditional on recorded cells with an EWMA issuance; it does not cover every scheduled origin.</p> : null}
    {report.coverage_basis === "recorded_current_version_baseline_attempts" ? <p>Coverage counts recorded current-version EWMA attempts; older unversioned attempts and missed origins are excluded.</p> : null}
    {report.coverage_basis === "recorded_current_version_candidate_cells" ? <p>Availability counts recorded current-version candidate cells, including cells without EWMA as failures. Older unversioned attempts and missed origins are excluded.</p> : null}
    {typeof report.coverage_basis === "string" && report.coverage_basis.includes("window") ? <p>This coverage counts recorded timestamped windows only; missed windows are not in the ledger.</p> : null}
    {report.replay_scheduled_units != null ? <p>Replay fit coverage {count(report.replay_baseline_available_units)}/{count(report.replay_scheduled_units)} scheduled units ({percent(report.replay_fit_coverage)}) · skipped before strikes: {reasonList(report.replay_rejection_reasons)}</p> : null}
    <p>Brier {units ? score(brier?.candidate) : "N/A"} (EWMA {units ? score(brier?.baseline) : "N/A"}); paired change {units ? score(brier?.paired_delta) : "N/A"}; 95% calendar-block interval {intervalText}</p>
    <p>Log loss {units ? score(logLoss?.candidate) : "N/A"} (EWMA {units ? score(logLoss?.baseline) : "N/A"}); paired change {units ? score(logLoss?.paired_delta) : "N/A"}</p>
    {data(report.quote_reanchored_comparator) ? <p>Matched quote-reanchored comparator: Brier {score(data(report.quote_reanchored_comparator)?.brier)} · log loss {score(data(report.quote_reanchored_comparator)?.log_loss)}</p> : null}
    {data(report.by_window) ? <p>Matched intraday windows: {Object.entries(data(report.by_window)!).map(([window, value]) => `${window} N=${count(data(value)?.scored_ticker_date_window_expiry_units)}`).join(" · ")}</p> : null}
    {(["call", "put"] as const).map((side) => {
      const bins = Array.isArray(calibration?.[side]) ? calibration[side] as unknown[] : []
      const nonempty = bins.map((item, index) => ({ item: data(item), index })).filter(({ item }) => (number(item?.count) ?? 0) > 0)
      return <p key={side}>{side === "call" ? "Call" : "Put"} calibration: {nonempty.length
        ? nonempty.map(({ item, index }) => `${index * 10}–${(index + 1) * 10}%: N=${count(item?.count)}, forecast ${percent(item?.forecast_mean)}, observed ${percent(item?.observed_rate)}`).join(" · ")
        : "N=0 · N/A"}</p>
    })}
    <p>{latency && "p50" in latency ? `Forecast p50/p95 ${milliseconds(latency.p50)}/${milliseconds(latency.p95)}` : `Preparation p50/p95 ${milliseconds(data(latency?.prepare)?.p50)}/${milliseconds(data(latency?.prepare)?.p95)} · lookup p50/p95 ${milliseconds(data(latency?.lookup)?.p50)}/${milliseconds(data(latency?.lookup)?.p95)}`}</p>
  </section>
}

function PhysicalResult({ model, selected, evidenceIndex }: { model: PredictiveOddsView; selected: PhysicalModel; evidenceIndex?: Data | null }) {
  const valid = predictiveAvailable(model)
  const evidence = data(model.model_evidence) ?? data(model.evidence_key ? evidenceIndex?.[model.evidence_key] : null)
  const support = model.support == null ? "support N/A" : model.method === "empirical_scaled"
    ? `${count(model.support)} historical horizon samples (overlapping)`
    : model.method === "student_t_ewma" || model.method === "gjr_garch_t"
      ? `${count(model.support)} simulated paths`
      : `${count(model.support)} completed returns`
  return <li className="model-result">
    <div className="model-result-heading"><strong>{physicalModelName(model.method)}</strong>{model.method === selected ? <span className="model-tag">Selected</span> : null}</div>
    <p>{valid ? `${unsignedPercentTenths(model.itm_pct_tenths)} ITM · ${unsignedPercentTenths(model.otm_pct_tenths)} OTM` : `${model.status === "pending" ? "Pending" : "Unavailable"}: ${model.reason ?? "No valid estimate"}`}</p>
    <p className="model-meta">{model.price_basis === "validated_underlying_quote" ? "Validated stock quote" : model.price_basis === "completed_close" ? "Completed stock close" : "Input basis unavailable"} · input {model.price_as_of ?? model.as_of_session ?? "date unavailable"} · expiry {model.expiry_session ?? "unknown"} · version {model.model_version ?? "unknown"} · {support}</p>
    <p className="model-meta">{model.method === "empirical_scaled" ? `Independent history blocks ${model.independent_blocks ?? "N/A"} (minimum 30)` : model.simulation_error_95_pct_tenths != null ? `Maximum 95% simulation error ±${(model.simulation_error_95_pct_tenths / 10).toFixed(1)} percentage points; model uncertainty excluded` : "Simulation precision N/A"} · fit {milliseconds(model.fit_ms)} · lookup {milliseconds(model.lookup_ms)} · data {model.data_hash?.slice(0, 12) ?? "hash unavailable"}</p>
    {evidencePanel("Prospective as-issued", evidence?.prospective)}
    {evidencePanel("Retrospective replay", evidence?.retrospective)}
  </li>
}

function MarketResult({ model }: { model: MarketOddsView }) {
  const evidence = data(model.model_evidence)
  const valid = oddsAvailable(model)
  const heldOut = number(evidence?.held_out_count) ?? 0
  const inside = number(evidence?.held_out_inside) ?? 0
  const check = (value: unknown) => value === true ? "yes" : value === false ? "no" : "N/A"
  return <li className="model-result">
    <div className="model-result-heading"><strong>{marketModelName(model.method)} · risk-neutral</strong>{model.method === "regimelib" ? <span className="model-tag">Benchmark</span> : null}</div>
    <p>{valid ? `${unsignedPercentTenths(model.itm_pct_tenths)} ITM · ${unsignedPercentTenths(model.otm_pct_tenths)} OTM` : `${model.status === "pending" ? "Pending" : "Unavailable"}: ${model.reason ?? "No valid estimate"}`}</p>
    <p className="model-meta">{quoteSupportLabel(model) ?? "Quote bounds N/A"} · session {model.session_date ?? "unavailable"} · version {model.model_version ?? "unknown"}</p>
    <p className="model-meta">Held-out quote coverage {heldOut ? `${inside}/${heldOut} (${percent(inside / heldOut)})` : "N=0 · N/A"} · paired quotes {count(evidence?.paired_held_out_count)} · bid/ask fit {check(evidence?.bid_ask_fit)} · one-tick stable {check(evidence?.one_tick_stable)}</p>
    {number(evidence?.paired_shadow_inside) != null && number(evidence?.paired_benchmark_inside) != null ? <p className="model-meta">Matched held-out fit: curve {count(evidence?.paired_shadow_inside)} vs benchmark {count(evidence?.paired_benchmark_inside)} of {count(evidence?.paired_held_out_count)} quotes.</p> : null}
    <p className="model-meta">Fit {milliseconds(evidence?.fit_ms)} · refresh {milliseconds(evidence?.refresh_ms)} · rejections: {reasonList(evidence?.rejection_reasons)}</p>
  </li>
}

export default function ModelComparison({ physical, market, selected, evidenceIndex }: {
  physical: PredictiveOddsView[] | null | undefined
  market: MarketOddsView[] | null | undefined
  selected: PhysicalModel
  evidenceIndex?: Data | null
}) {
  const [open, setOpen] = useState(false)
  const comparable = physical?.filter((model) => predictiveAvailable(model) && model.price_basis === "completed_close" && model.itm_pct_tenths != null) ?? []
  const disagreement = comparable.length >= 2
    ? ((Math.max(...comparable.map((model) => model.itm_pct_tenths!)) - Math.min(...comparable.map((model) => model.itm_pct_tenths!))) / 10).toFixed(1)
    : null
  return <details className="model-comparison">
    <summary onClick={() => setOpen((current) => !current)}>Compare models</summary>
    {open ? <div className="model-comparison-body">
      <section aria-label="Physical forecast models"><h4>Stock-close forecasts</h4><p>Real-world expiry-close odds. Your selection sets the compact odds and hypothetical risk; accuracy evidence remains separate.</p>
        <p>Model spread: {disagreement == null ? "N/A (fewer than two comparable estimates)" : `${disagreement} percentage points across ${comparable.length} completed-close models`}. This shows method sensitivity, not forecast accuracy.</p>
        <ul>{physical?.length ? physical.map((model, index) => <PhysicalResult key={model.method ?? index} model={model} selected={selected} evidenceIndex={evidenceIndex} />) : <li>No physical model results yet.</li>}</ul>
      </section>
      <section aria-label="Risk-neutral market models"><h4>Option-price estimates</h4><p>Risk-neutral odds from option quotes. Quote fit does not measure realized forecast accuracy.</p>
        <ul>{market?.length ? market.map((model, index) => <MarketResult key={model.method ?? index} model={model} />) : <li>No market model results yet.</li>}</ul>
      </section>
    </div> : null}
  </details>
}
