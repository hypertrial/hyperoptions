// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, expect, it } from "vitest"
import type { MarketOddsView, PredictiveOddsView } from "./generated/types.gen"
import ModelComparison from "./ModelComparison"

afterEach(cleanup)

it("shows all physical and market methods, including pending and unavailable results", () => {
  const physical: PredictiveOddsView[] = [
    { method: "lognormal_ewma", status: "available", itm_pct_tenths: 600, otm_pct_tenths: 400, atm_pct_tenths: 0, price_basis: "completed_close", support: 60 },
    { method: "empirical_scaled", status: "available", itm_pct_tenths: 630, otm_pct_tenths: 370, atm_pct_tenths: 0, price_basis: "completed_close", support: 210, independent_blocks: 35 },
    { method: "student_t_ewma", status: "pending", reason: "candidate_not_prepared", fit_ms: 20 },
    { method: "gjr_garch_t", status: "unavailable", reason: "needs_500_sessions" },
    { method: "intraday_shadow", status: "unavailable", reason: "stale_quote" },
  ]
  const market: MarketOddsView[] = [
    { method: "regimelib", status: "available", itm_pct_tenths: 520, otm_pct_tenths: 480, bound_low_pct_tenths: 400, bound_high_pct_tenths: 600 },
    { method: "constrained_call_curve", status: "unavailable", reason: "clean_strikes_do_not_bracket_contract" },
  ]
  render(<ModelComparison physical={physical} market={market} selected="student_t_ewma" />)
  const trigger = screen.getByRole("button", { name: "Compare models" })
  expect(screen.queryByRole("dialog")).toBeNull()
  fireEvent.click(trigger)
  expect(screen.getByRole("dialog", { name: "Compare models" })).toBeTruthy()
  const physicalSection = screen.getByRole("region", { name: "Physical forecast models" })
  expect(physicalSection.textContent).toContain("Student-t EWMA")
  expect(physicalSection.textContent).toContain("Student-t EWMASelected")
  expect(screen.getByRole("region", { name: "Physical forecast models" }).textContent).toContain("Model spread: 3.0 percentage points across 2 completed-close models")
  expect(physicalSection.querySelectorAll(".model-result details[open]")).toHaveLength(0)
  fireEvent.click(screen.getByText("Scaled empirical"))
  expect(screen.getByRole("region", { name: "Physical forecast models" }).textContent).toContain("Independent history blocks 35 (minimum 30)")
  expect(screen.getByRole("region", { name: "Physical forecast models" }).textContent).toContain("needs_500_sessions")
  expect(screen.getByRole("region", { name: "Risk-neutral market models" }).textContent).toContain("Regimelib")
  expect(screen.getByRole("region", { name: "Risk-neutral market models" }).textContent).toContain("clean_strikes_do_not_bracket_contract")
  fireEvent.click(screen.getByText("Regimelib"))
  expect(screen.getByRole("dialog").textContent).toContain("Quote bounds")
  fireEvent.click(screen.getByRole("button", { name: "Close model comparison" }))
  expect(screen.queryByRole("dialog")).toBeNull()
  expect(document.activeElement).toBe(trigger)
})

it("reports simulation precision as sampling error rather than forecast confidence", () => {
  render(<ModelComparison physical={[{
    method: "student_t_ewma", status: "available", itm_pct_tenths: 610, otm_pct_tenths: 390,
    atm_pct_tenths: 0,
    price_basis: "completed_close", support: 4096, simulation_error_95_pct_tenths: 16,
    fit_ms: 13, lookup_ms: 2, data_hash: "abc123456789more",
  }]} market={[]} selected="student_t_ewma" />)
  fireEvent.click(screen.getByText("Compare models"))
  fireEvent.click(screen.getByText("Student-t EWMA"))
  const physical = screen.getByRole("region", { name: "Physical forecast models" }).textContent
  expect(physical).toContain("4,096 terminal-price scenarios")
  expect(physical).toContain("Maximum 95% simulation error ±1.6 percentage points; model uncertainty excluded")
  expect(physical).toContain("Model spread: N/A")
  expect(physical).toContain("data abc123456789")
})

it("keeps retrospective and as-issued evidence distinct and reports N=0 without confidence", () => {
  const empty = {
    generated_at: "2026-09-27T12:00:00Z", model_version: "student-v1", input_version: "ledger-v1",
    tickers: 0, independent_date_blocks: 0, ticker_origin_horizon_units: 0,
    contract_forecasts_available: 0, contract_cells_attempted: 0,
    coverage_basis: "recorded_current_version_candidate_cells",
    brier: { baseline: null, candidate: null, paired_delta: null, bootstrap_95: null },
    log_loss: { baseline: null, candidate: null, paired_delta: null },
    calibration_by_side: { call: [], put: [] }, latency_ms: {}, rejection_reasons: {},
  }
  const physical: PredictiveOddsView[] = [{
    method: "student_t_ewma", status: "available", itm_pct_tenths: 610, otm_pct_tenths: 390, atm_pct_tenths: 0,
    evidence_key: "student_t_ewma:2-5", model_evidence: null,
  }]
  const replay = { ...empty, independent_date_blocks: 20, ticker_origin_horizon_units: 500, tickers: 20, coverage_basis: "recorded_contract_cells_with_baseline_issuance", replay_scheduled_units: 25, replay_baseline_available_units: 20, replay_fit_coverage: 0.8, replay_rejection_reasons: { split_affected: 5 }, brier: { baseline: 0.22, candidate: 0.20, paired_delta: -0.02, bootstrap_95: [-0.03, -0.01] } }
  render(<ModelComparison physical={physical} market={[]} selected="student_t_ewma" evidenceIndex={{ "student_t_ewma:2-5": { prospective: empty, retrospective: replay } }} />)
  fireEvent.click(screen.getByText("Compare models"))
  fireEvent.click(screen.getByText("Student-t EWMA"))
  fireEvent.click(screen.getByText(/Prospective as-issued · N=0/))
  fireEvent.click(screen.getByText(/Retrospective replay · N=500/))
  const prospective = screen.getByRole("region", { name: "Prospective as-issued evidence" })
  const replayPanel = screen.getByRole("region", { name: "Retrospective replay evidence" })
  expect(prospective.textContent).toContain("N=0")
  expect(prospective.textContent).toContain("Brier N/A")
  expect(prospective.textContent).toContain("significance not estimable")
  expect(prospective.textContent).toContain("including cells without EWMA as failures")
  expect(replayPanel.textContent).toContain("Current-vintage screening")
  expect(replayPanel.textContent).toContain("[-0.030, -0.010]")
  expect(replayPanel.textContent).toContain("20/25 scheduled units (80.0%)")
  expect(replayPanel.textContent).toContain("split_affected: 5")
  expect(replayPanel.textContent).toContain("Call calibration: N=0")
  expect(replayPanel.textContent).toContain("Put calibration: N=0")
})

it("keeps the selected physical model first without hiding valid alternatives or risk-neutral methods", () => {
  const physical: PredictiveOddsView[] = [
    { method: "iv_physical", status: "unavailable", reason: "rights_cleared_option_history_unavailable" },
    { method: "ngboost_pooled", status: "pending", reason: "candidate_not_prepared" },
    { method: "markov_switching", status: "available", itm_pct_tenths: 570, otm_pct_tenths: 430, atm_pct_tenths: 0, price_basis: "completed_close" },
    { method: "earnings_jump", status: "unavailable", reason: "verified_release_time_history_unavailable" },
    { method: "ohlc_har", status: "available", itm_pct_tenths: 610, otm_pct_tenths: 390, atm_pct_tenths: 0, price_basis: "completed_close" },
    { method: "skew_t_ewma", status: "unavailable", reason: "skew_t_fit_failed" },
    { method: "egarch_skew_t", status: "unavailable", reason: "egarch_parameters_invalid" },
  ]
  const market: MarketOddsView[] = [
    { method: "ssvi", status: "unavailable", reason: "rights_cleared_option_history_unavailable" },
    { method: "constrained_call_curve", status: "pending", reason: "curve_fit_pending" },
    { method: "regimelib", status: "available", itm_pct_tenths: 540, otm_pct_tenths: 460 },
  ]
  render(<ModelComparison physical={physical} market={market} selected="egarch_skew_t" />)
  fireEvent.click(screen.getByRole("button", { name: "Compare models" }))
  const dialog = screen.getByRole("dialog", { name: "Compare models" })
  const physicalRows = Array.from(dialog.querySelectorAll('[aria-label="Physical forecast models"] .model-result-heading'))
  expect(physicalRows.map((row) => row.querySelector("strong")?.textContent)).toEqual([
    "EGARCH skewed-tSelected", "Two-regime switching variance", "Daily OHLC range/HAR proxy",
    "Pooled NGBoost", "IV-informed forecast", "Earnings jump", "Skewed-t EWMA",
  ])
  expect(physicalRows[0].textContent).toContain("Unavailable · egarch parameters invalid")
  expect(physicalRows[1].textContent).toContain("57.0% ITM · 43.0% OTM")
  const marketRows = Array.from(dialog.querySelectorAll('[aria-label="Risk-neutral market models"] .model-result-heading'))
  expect(marketRows.map((row) => row.querySelector("strong")?.textContent)).toEqual([
    "RegimelibBenchmark", "Constrained call curve", "SSVI volatility surface",
  ])
  expect(marketRows[2].textContent).toContain("rights cleared option history unavailable")
  expect(dialog.textContent).not.toContain("confidence score: ")
})

it("shows adjusted matched evidence and CRPS only as evidence, not as odds", () => {
  const report = {
    generated_at: "2026-09-27T12:00:00Z", model_version: "ohlc-har-v1", input_version: "frozen-v1",
    tickers: 20, independent_date_blocks: 20, ticker_origin_horizon_units: 500,
    contract_forecasts_available: 500, contract_cells_attempted: 525,
    brier: { baseline: 0.24, candidate: 0.20, paired_delta: -0.04,
      bootstrap_95: [-0.06, -0.02], bootstrap_familywise_95: [-0.08, -0.01], comparison_count: 10 },
    log_loss: { baseline: 0.66, candidate: 0.61, paired_delta: -0.05 },
    crps: { baseline: 3.4, candidate: 3.1, scored_units: 480 },
    calibration_by_side: { call: [], put: [] }, latency_ms: {}, rejection_reasons: { missing_bar: 25 },
  }
  render(<ModelComparison physical={[{
    method: "ohlc_har", status: "available", itm_pct_tenths: 610, otm_pct_tenths: 390,
    atm_pct_tenths: 0, evidence_key: "ohlc_har:2-5",
  }]} market={[]} selected="ohlc_har" evidenceIndex={{ "ohlc_har:2-5": {
    prospective: report, retrospective: { ...report, independent_date_blocks: 19 },
  } }} />)
  fireEvent.click(screen.getByRole("button", { name: "Compare models" }))
  fireEvent.click(screen.getByText("Daily OHLC range/HAR proxy"))
  fireEvent.click(screen.getByText(/Prospective as-issued · N=500/))
  fireEvent.click(screen.getByText(/Retrospective replay · N=500/))
  const prospective = screen.getByRole("region", { name: "Prospective as-issued evidence" })
  const retrospective = screen.getByRole("region", { name: "Retrospective replay evidence" })
  expect(prospective.textContent).toContain("Bonferroni-adjusted 95% within-band calendar-block interval (10 methods) [-0.080, -0.010]")
  expect(prospective.textContent).toContain("CRPS 480 scored units · model 3.100 · EWMA 3.400")
  expect(prospective.textContent).toContain("Coverage 500/525 recorded contract cells (95.2%)")
  expect(prospective.textContent).toContain("missing_bar: 25")
  expect(retrospective.textContent).toContain("Current-vintage screening; not an as-issued accuracy claim")
  expect(retrospective.textContent).toContain("significance not estimable")
})

it("shows missed capture windows separately from scored intraday coverage", () => {
  render(<ModelComparison physical={[{
    method: "intraday_shadow", status: "available", itm_pct_tenths: 610,
    otm_pct_tenths: 390, atm_pct_tenths: 0, support: 60,
    evidence_key: "intraday_shadow:1",
  }]} market={[]} selected="intraday_shadow" evidenceIndex={{
    "intraday_shadow:1": { prospective: {
      coverage_basis: "recorded_intraday_contract_windows",
      capture_windows_all_horizons: { expected: 5, captured: 3, missed: 1, pending: 1 },
      ticker_origin_horizon_units: 0,
    } },
  }} />)
  fireEvent.click(screen.getByRole("button", { name: "Compare models" }))
  fireEvent.click(screen.getByText("Intraday conditioned"))
  fireEvent.click(screen.getByText(/Prospective as-issued · N=0/))
  const details = screen.getByRole("region", { name: "Prospective as-issued evidence" })
  expect(details.textContent).toContain("Capture windows, all horizons (last 35 days): 3/5 captured · 1 missed · 1 pending")
  expect(screen.getByText(/60 completed returns/)).toBeTruthy()
})
