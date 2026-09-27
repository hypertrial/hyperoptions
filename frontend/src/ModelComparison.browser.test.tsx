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
  const { container } = render(<ModelComparison physical={physical} market={market} selected="student_t_ewma" />)
  const disclosure = container.querySelector("details")!
  expect(disclosure.open).toBe(false)
  fireEvent.click(screen.getByText("Compare models"))
  expect(disclosure.open).toBe(true)
  expect(screen.getByRole("region", { name: "Physical forecast models" }).textContent).toContain("Student-t EWMA")
  expect(screen.getByRole("region", { name: "Physical forecast models" }).textContent).toContain("Student-t EWMASelected")
  expect(screen.getByRole("region", { name: "Physical forecast models" }).textContent).toContain("Model spread: 3.0 percentage points across 2 completed-close models")
  expect(screen.getByRole("region", { name: "Physical forecast models" }).textContent).toContain("Independent history blocks 35 (minimum 30)")
  expect(screen.getByRole("region", { name: "Physical forecast models" }).textContent).toContain("needs_500_sessions")
  expect(screen.getByRole("region", { name: "Risk-neutral market models" }).textContent).toContain("Regimelib")
  expect(screen.getByRole("region", { name: "Risk-neutral market models" }).textContent).toContain("clean_strikes_do_not_bracket_contract")
  expect(disclosure.textContent).toContain("Quote bounds")
})

it("reports simulation precision as sampling error rather than forecast confidence", () => {
  render(<ModelComparison physical={[{
    method: "student_t_ewma", status: "available", itm_pct_tenths: 610, otm_pct_tenths: 390,
    price_basis: "completed_close", support: 4096, simulation_error_95_pct_tenths: 16,
    fit_ms: 13, lookup_ms: 2, data_hash: "abc123456789more",
  }]} market={[]} selected="student_t_ewma" />)
  fireEvent.click(screen.getByText("Compare models"))
  const physical = screen.getByRole("region", { name: "Physical forecast models" }).textContent
  expect(physical).toContain("4,096 simulated paths")
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
