# Reproducible historical option research

The offline suite validates actual-strike expiry forecasts, evaluates predetermined
covered calls and cash-secured puts, and audits observed liquidity and assumption
sensitivity. It uses collected local data and existing dependencies. Results are
**current-vintage retrospective research**, with no automatic model promotion or
change to app rankings.

From `backend/`, using the installed environment:

```sh
uv run --group research python scripts/evaluate_historical_options.py freeze
uv run --group research python scripts/evaluate_historical_options.py run
uv run --group research python scripts/evaluate_historical_options.py run \
  --output ../.local/research/historical-options/run-v1-repeat
```

`freeze` accepts `--source`, `--prices`, `--flat-cache`, and `--output`; `run`
accepts `--snapshot` and `--output`. Default artifacts live beneath the gitignored
`.local/research/historical-options` directory. Every destination must be absent;
conflicting or incomplete destinations fail without overwriting them. Use a fresh
path for a retry. A freeze detecting source changes must be retried after writers
stop. No command downloads data, refreshes prices, opens an app database helper,
modifies source databases, or provisions a model.

The freeze opens canonical `massive` base tables directly in a read-only DuckDB
transaction, with external access and automatic extension loading disabled. Fixed
selections stream through Arrow to Parquet. Stock cache schemas, manifests and
content hashes are validated; the validated in-memory frames are frozen and
fingerprinted again before publication. REST daily validation, filtered flat-file
daily validation and hourly-derived marks retain separate provenance. Flat-cache
records can repair missing validation evidence locally, without changing the
source database. Ordinary encoded root/side/expiry/strike and 100-share terms are
required. Fractional hourly strikes must agree with reference and encoded strikes.

The snapshot preserves all archive-row accounting, timestamps, adjustments,
invalid/duplicate observations, reconciliation failures, fetch gaps, stale windows
and unknown coverage. Hourly bars are primary. Regular-session activity and the
last observed regular-session mark are derived with exchange calendars and
America/New_York time, including DST and early closes. A last observed hourly mark
is a closing-mark proxy; a missing bar never becomes a zero price or proof of no
listing. Today's reference catalogue does not establish historical completeness.

The primary four-ticker panel spans October 29, 2024–December 31, 2025. A separate
2026 supplement uses CIFR, IREN and WULF through their shared verified September
28 cutoff. NBIS 2026 appears only in the archive audit. Other eligible dates are
separate. All archive liquidity through October 2, 2026 is audited. Native model
warmup remains required. CIFR history begins at its verified August 30, 2021
identity boundary; WULF begins December 14, 2021. IREN and NBIS use their first
verified stock observations. Identity evidence is recorded in the
[Cipher SEC filing](https://www.sec.gov/Archives/edgar/data/1819989/000095017022002861/cifr-20211231.htm)
and [TeraWulf filing](https://investors.terawulf.com/sec-filings/all-sec-filings/content/0000950142-21-003986/eh210209806_ex9901.htm).
No corporate-action history is invented to qualify NBIS 2026.

Forecast origins occur after scheduled close, using only completed history.
EWMA and seven historical challengers retain their native mathematics, versions,
seeds, numerical validity, convergence and iteration limits. The research-only
`enforce_fit_latency=False` setting bypasses elapsed-time rejection; live defaults
remain `True`. Methods needing additional qualified inputs remain unavailable.
Actual contracts at exact 1–25 session expiry horizons are evaluated, with strict
call `close > strike`, strict put `close < strike`, and ATM reported separately.
Brier, log loss, calibration bins, stock CRPS and spot-normalized CRPS retain
metric-specific availability counts. Unbounded extreme scenarios are not clipped
to improve scores.

Downside predictions and maturity labels use the **same decision Close and option
mark**. The original forecast anchor remains intact. A research-only cumulative
scenario-weight and price-moment integrator calculates mean P&L, loss probability
and weighted-left fifth-percentile P&L efficiently across strikes. Pinball loss,
strict and inclusive quantile breaches and payoff atoms are retained. Next-session
entry prices belong only to the economics study.

Strategy selection grain is ticker × decision session × side × exact horizon ×
policy. Positive option mark/activity and valid stock Close must be from that
exact session; no forward fill or unavailable historical OI/spread filter applies.
For `r = strike / stock Close`, the fixed policies are:

| Policy | Covered calls | Cash-secured puts |
| --- | --- | --- |
| App maximum displayed APR | `0.90 ≤ r < 1` | `0.90 ≤ r < 1` |
| Near ATM default side | Closest below spot, `0.975 ≤ r < 1` | Same |
| Fixed 5% below spot | Closest to 0.95 in `[0.925, 0.975]` | Same |
| Wider comparator | Closest to 1.05 in `[1.025, 1.075]` | Closest to 0.90 in `[0.875, 0.925]` |

APR uses app time-value premium, calendar DTE, and native Decimal HALF_UP scaled
percentage. Valid negative APR remains eligible. APR ties use decision volume
descending, strike ascending, then identifier; target policies rank distance
first and then these ties. Selection sees no entry-session or outcome data.

Entry uses next-session official stock Open and the selected option's Open from
the hour containing the regular-session open (normally 09:00–10:00 ET, containing
09:30). Bucket intervals are retained; actual first-trade time is unknown. A
missing opening bucket is `no_entry`: no replacement contract, later hour or
stale mark. Later action exclusions retain the selection without replacement.
Unverifiable split/strike bases, including splits in the frozen stock vintage,
are excluded. Scored economics are conditional on verified action-safe intervals.

Covered-call P&L is `100 × [min(expiry Close, strike) − entry stock + premium] −
fee`; put P&L is `100 × [premium − max(strike − expiry Close, 0)] − fee`. Gross
committed capital is `100 × entry stock` for calls and `100 × strike` for puts;
net outlay is separate. Calls compare with the same 100-share stock entry and
slippage; puts compare with zero-yield nominal cash. Reports retain capital
returns, loss frequency, lower tails, expiry-close ITM and call upside forgone.
Overlapping trades are independent experiments: no compounded CAGR or portfolio
drawdown is computed.

Selections remain fixed across premium haircuts 0/5/10/20%, option fees $0/$0.65
and covered-call stock-entry slippage 0/10/25 bp. Put results do not duplicate
irrelevant stock slippage. Fees exceeding premium remain negative. Haircuts are
stress assumptions, not measured bid/ask spreads. Separate ex-ante activity
screens require at least five decision-session transactions and twenty contracts
of volume. Ranking stability uses decision marks under haircuts. Timing diagnostics
use the same selected contract at opening, 10:00, 13:00 and 15:00 ET, where those
buckets fall within the session, while keeping stock entry fixed. Missing buckets
stay missing. This is premium-mark sensitivity, with no optimal-hour or
synchronous-execution claim.

All sessions remain in descriptive reports. Inference uses the final 25% of each
panel's exchange-calendar sessions as holdout. Development outcomes mature
strictly before its boundary; holdout origins begin on or after it. Common
calendar schedules across all tickers are spaced 2, 6 and 26 sessions for bands
1, 2–5 and 6–25. Contract scores sharing a stock outcome are averaged before
inference. Duplicated policy selections do not increase independent forecast
counts. Each challenger is matched to its own EWMA contract cells; scores on
unequal availability sets are not ranked against one another.

Complete paired date clusters, including all tickers together, are bootstrapped
2,000 times with seed 1729. At least twenty scored paired dates are required for
intervals; otherwise results are `not_estimable`. Seven-comparison familywise
adjustment applies only to predeclared pooled challenger/EWMA Brier comparisons
within each core holdout band. Policy, supplement and other metric comparisons
remain exploratory. Sparse long-horizon holdout support is an expected result.

A versioned manifest binds source hashes, schemas/counts, verified ranges,
settings, code, dependency/model versions and seeds. Detail Parquet, `summary.json`
and `report.md` are deterministic analytical artifacts. `resource.json` records
runtime and peak RSS separately; measured durations and generation timestamps do
not enter analytical hashes. Snapshots and runs publish atomically only after
integrity checks. A fresh replay must reproduce canonical analytical hashes.
The batch-processing memory target is 4 GiB peak RSS, never a model acceptance
deadline. Credentials, environment dumps, machine paths, raw data, generated
research artifacts and Pad content stay out of Git.

Option trade bars do not establish executable quotes, spreads or synchronous
stock/option fills. Expiry-close ITM is not observed assignment. Current-vintage
prices may include later corrections and split normalization. Findings cannot
establish as-issued forecasts or actual portfolio performance. No historical
result automatically promotes a model or changes the live app.
