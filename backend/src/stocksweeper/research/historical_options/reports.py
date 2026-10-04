"""Deterministic, local research reports with explicit evidence limits."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path


def json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"unsupported report value: {type(value).__name__}")


def write_summary(output: Path, summary: dict) -> None:
    (output / "summary.json").write_text(
        json.dumps(summary, sort_keys=True, indent=2, allow_nan=False,
                   default=json_default) + "\n", encoding="utf-8")


def _table(rows: list[dict], columns: tuple[str, ...]) -> str:
    if not rows:
        return "No qualified observations.\n"
    def value(item):
        if item is None:
            return "—"
        if isinstance(item, float):
            return f"{item:.6g}"
        return str(item).replace("|", "\\|").replace("\n", " ")
    return "\n".join([
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
        *["| " + " | ".join(value(row.get(col)) for col in columns) + " |" for row in rows],
    ]) + "\n"


def write_report(output: Path, summary: dict) -> None:
    studies = summary["studies"]
    lines = [
        "# Historical option research",
        "", "Current-vintage retrospective research. All-session descriptions and the "
        "separately scheduled inference panel use the frozen source vintage. No result "
        "automatically changes a model or app ranking.", "",
        "Option OHLC values are trade-bar proxies with unknown individual trade times. "
        "They are not executable quotes. Expiry-close ITM labels are not actual "
        "assignment. Overlapping experiments are not portfolio performance.", "",
        "## Coverage and exclusions", "",
        "The four-ticker primary panel is 2024-10-29 through 2025-12-31. The separate "
        "2026 supplement includes CIFR, IREN and WULF through 2026-09-28. NBIS 2026 "
        "remains in archive activity counts and cannot enter forecast or payoff scoring.", "",
        "Missing bars do not imply zero prices, unavailable listings or known nontrading. "
        "The current reference catalogue does not establish historical listing completeness. "
        "The normalized snapshot retains source lineage and every exclusion.", "",
        "Snapshot analytical hash: `" + summary["snapshot_hash"] + "`", "",
        "```json", json.dumps(summary.get("coverage", {}), sort_keys=True, indent=2,
                             default=json_default), "```", "",
        "## A. Forecast and downside calibration", "",
        "Decision forecasts use completed stock history after the scheduled session close. "
        "Predicted and realized payoff labels both use that session's stock Close and "
        "option closing-mark proxy. Each challenger has its own matched EWMA cells. "
        "Extreme scenarios remain intact; invalid numerical metrics retain reasons.", "",
        "Offline availability bypasses only fit elapsed-time rejection. Numerical validity, "
        "convergence, iteration limits, model versions and distribution mathematics persist. "
        "Methods needing additional qualified inputs are listed unavailable.", "",
        "```json", json.dumps(studies["calibration"], sort_keys=True, indent=2,
                             default=json_default), "```", "",
        "All-session one-session downside calibration (descriptive availability sets; "
        "these raw scores are not a model ranking):", "",
        _table([r for r in summary["forecast_descriptives"] if r["panel"] == "primary"
                and r["quality"] == "primary" and r["band"] == "1"],
               ("model", "side", "brier_scored", "brier_mean", "loss_probability_mean",
                "realized_loss_rate", "pinball_mean", "breach_strict_rate",
                "breach_inclusive_rate")), "",
        "All-session forecast diagnostics (separate panels and availability sets):", "",
        _table([r for r in summary["forecast_descriptives"] if r["quality"] == "primary"
                and r["panel"] in {"primary", "supplement"}],
               ("panel", "model", "side", "band", "attempted_contract_cells",
                "brier_scored", "brier_mean", "log_loss_mean", "crps_mean",
                "normalized_crps_mean", "atm_rate")), "",
        "Paired expected P&L and lower-tail diagnostics (per share, before costs):", "",
        _table([r for r in summary["forecast_descriptives"] if r["quality"] == "primary"
                and r["panel"] in {"primary", "supplement"}],
               ("panel", "model", "side", "band", "pnl_paired_cells",
                "paired_expected_pnl_mean", "paired_realized_pnl_mean", "paired_pnl_bias",
                "paired_pnl_absolute_error", "loss_probability_mean", "realized_loss_rate",
                "quantile05_mean", "pinball_mean", "breach_strict_rate",
                "breach_inclusive_rate", "atom_mean", "quantile_atom_mean")), "",
        "Expiry probability calibration bins (bin 0 is [0, 0.1), bin 9 includes 1):", "",
        _table([r for r in summary["calibration_bins"] if r["primary_eligible"]
                and r["panel"] in {"primary", "supplement"}],
               ("panel", "model", "side", "bin", "contract_cells", "forecast_mean",
                "observed_rate")), "",
        "Loss probability calibration bins:", "",
        _table([r for r in summary["loss_calibration_bins"] if r["primary_eligible"]
                and r["panel"] in {"primary", "supplement"}],
               ("panel", "model", "side", "bin", "contract_cells", "forecast_mean",
                "observed_rate")), "",
        "Complete reconciliation inclusion sensitivity and other eligible dates remain "
        "separate in summary.json and forecast_cells.parquet.", "",
        "Core holdout paired Brier comparisons (challenger minus EWMA; lower is better):", "",
        _table([r for r in summary["forecast_inference"] if r["panel"] == "primary"
                and r["period"] == "holdout" and r["metric"] == "brier"],
               ("model", "band", "matched_contract_cells", "paired_dates", "paired_delta",
                "status", "interval", "multiplicity")), "",
        "All other metrics, development comparisons and supplement results are exploratory. "
        "Intervals require twenty scored paired dates, resample complete calendar-date "
        "clusters with all tickers together (2,000 draws, seed 1729), and use common "
        "2/6/26-session schedules. Shared-close contract scores are averaged first. "
        "Development outcomes mature strictly before the last-quarter holdout boundary. "
        "The seven-comparison adjustment applies only to pooled core holdout Brier "
        "within each band. Limited long-horizon support is a completed research result.", "",
        "## B. Predetermined selections and expiry economics", "",
        "Four fixed strike policies use only exact decision-session stock Close, option "
        "closing marks and activity. Calendar DTE and native HALF_UP displayed APR "
        "determine APR ranking; negative valid APR is retained. Ties use previous-session "
        "volume descending, strike ascending, then contract identity.", "",
        "Entry uses the next official stock Open and the selected option's opening-hour "
        "Open, normally 09:00–10:00 ET containing 09:30. Its first-trade time is unknown. "
        "Missing buckets produce no entry without a replacement. Later corporate-action "
        "exclusions retain the original selection; scored economics are conditional on "
        "verified action-safe intervals and stock/strike price bases.", "",
        "Covered-call P&L is 100 × [min(expiry Close, strike) − stock entry + premium] "
        "− option fee. Put P&L is 100 × [premium − max(strike − expiry Close, 0)] "
        "− fee. Gross capital is 100 shares at entry for calls and 100 × strike for puts; "
        "net outlay is separate. Calls compare with the same stock entry and slippage; "
        "puts compare with zero-yield nominal cash.", "",
        "```json", json.dumps({k: v for k, v in studies["strategies"].items()
                         if k not in {"cost_grid", "ranking_stability", "timing_sensitivity"}},
                        sort_keys=True, indent=2,
                             default=json_default), "```", "",
        "Primary all-session gross outcomes (unscreened, zero costs; independent "
        "capital-normalized experiments):", "",
        _table([r for r in studies["strategies"]["cost_grid"]
                if r["panel"] == "primary" and r["quality"] == "primary"
                and r["screen"] == "unscreened" and r["haircut"] == 0
                and r["fee"] == 0 and r["stock_slippage_bps"] == 0],
               ("policy", "side", "scored_experiments", "mean_return",
                "mean_benchmark_return", "mean_excess_return", "loss_frequency",
                "fifth_percentile_return", "expiry_close_itm", "mean_upside_forgone")), "",
        "Complete cost-grid, screen, ranking and timing diagnostics are in summary.json "
        "and their corresponding detail/summary Parquet tables.", "",
        "Fixed selections are stressed with premium haircuts 0/5/10/20%, entry fees "
        "$0/$0.65, and covered-call stock slippage 0/10/25 bp. Fees exceeding premium "
        "remain negative. Haircuts are assumptions, not measured spreads. These independent "
        "experiments do not support compounded CAGR or portfolio drawdown.", "",
        "## C. Liquidity, support and robustness", "",
        "```json", json.dumps(studies["liquidity"], sort_keys=True, indent=2,
                             default=json_default), "```", "",
        "Archive activity covers the complete collected archive through 2026-10-02, "
        "including dates outside the scored panels. Strike distance is reported only "
        "where verified stock prices exist. Sparse contract-days, single-transaction "
        "bars, opening availability, reconciliation and unknown/stale fetch windows "
        "remain visible. No pre-first-observation interval is counted as known nontrading.", "",
        "Activity screens are ex ante using previous-session transactions and volume. "
        "Ranking diagnostics apply haircuts to decision marks. Premium-mark timing "
        "diagnostics keep the same selected contract and stock entry reference at opening, "
        "10:00, 13:00 and 15:00 ET where the session permits. Missing buckets stay missing. "
        "They establish neither an optimal selling hour nor synchronous execution.", "",
        "## Reproduction and limitations", "",
        "The versioned manifest binds source hashes, schemas, counts, stock ranges, "
        "settings, code and dependency/model versions. Detail Parquet tables and "
        "summary.json contain the complete analytical results; resource.json records "
        "runtime and peak RSS separately from canonical analytical hashes. Both commands "
        "publish atomically to absent destinations and verify hashes before replay.", "",
        "This retrospective source vintage can reflect revisions and split-normalization. "
        "No as-issued, executable-spread, historical OI, assignment or portfolio claim is "
        "supported. See docs/historical-options-research.md for commands and methodology.", "",
    ]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
