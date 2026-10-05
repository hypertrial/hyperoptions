# Next wheel-leg characteristics

This offline study identifies historical strike-depth, expiry, premium and stock
conditions for the next ITM covered call or OTM cash-secured put. It starts calls
from the current market value of already-owned shares and puts from reserved
cash. Original purchase cost and existing unrealized P&L have no role. Assignment
is neutral, shares remaining at expiry are valued at market, and contracts are
not rolled. This is a next-leg study, not a sequential wheel simulation.

From `backend/`, with the existing research environment and frozen snapshot:

```sh
uv run --group research python scripts/evaluate_historical_options.py wheel
uv run --group research python scripts/evaluate_historical_options.py wheel \
  --snapshot ../.local/research/historical-options/snapshot-v2 \
  --output ../.local/research/historical-options/wheel-v1-repeat
```

Defaults are `snapshot-v2` and `wheel-v1` under the existing ignored research
directory. Outputs must be absent. The command verifies snapshot hashes, reads
only frozen local files, and publishes the completed run atomically. It never
downloads data, changes the snapshot, refreshes prices, fits forecasters, or
changes the app. Existing `freeze` and `run` commands retain their behavior.

## Selection and assessment

All eligible observed contracts at exact 1–25 trading-session horizons are
described. Calls must be ITM and puts OTM at the prior-close decision: both
require `strike < stock Close`. Strike depth `1 - strike/Close` uses bands
`(0, 2.5%)`, `[2.5%, 5%)`, `[5%, 10%)`, `[10%, 20%)`, and `[20%, 100%)`.
Expiry bands are 1, 2–5, 6–10 and 11–25 trading sessions.

Within each ticker, decision session, side and depth/expiry band, select one
contract by highest decision-only net capped gain per gross-capital assessment
day. For calls, capped gain subtracts intrinsic value from stressed premium; for
puts it is stressed premium. Both subtract the $0.65 contract fee. Ties prefer
more transactions, more volume, lower strike and then contract identifier.
Negative valid rewards remain in descriptions and selection accounting.

Entry uses the following session's stock Open and the selected contract's
opening-hour Open proxy. If stock Open is at or below strike, the contract no
longer has the required moneyness and receives `no_entry`. Missing opening marks
also receive `no_entry`; neither case substitutes another contract. Later
reconciliation or corporate-action exclusions preserve the original choice.
The conservative action-safe exclusions are inherited from the original suite.
When a known opening strike crossing intentionally cancels the sale, valid
action-safe stock outcomes remain in whole-opportunity results: calls retain
shares, puts retain cash, and neither pays an option fee. Missing opening prices
remain unknown. At least twenty actual selling dates and four traded expiries
are needed for a selling recommendation; passive stock gains cannot qualify.

Covered-call expiry P&L is `100 * (min(expiry Close, strike) - current stock
value + premium) - fee`. Put P&L is `100 * (premium - max(strike - expiry Close,
0)) - fee`. These include unrealized loss in shares still held or acquired at
expiry. Ending cash and share value are reported separately. ATM is economically
well-defined but assignment is ambiguous.

Gross capital is current value of 100 shares for calls, or `100 * strike` for
puts. Net premium-adjusted outlay is separate. Holding days are calendar dates
from entry through expiry, inclusive, minimum one. Reward/day is the sum of
capital-normalized returns divided by the sum of holding days, not an average
of annualized APRs. Expiry assessment does not imply that capital becomes free
for another trade. Call benchmarks retain the same shares; put benchmarks retain
zero-yield nominal cash. Secondary buy-write columns add 10/25 bp stock purchase
slippage; already-owned shares incur no fictional acquisition cost.

Reference results use a 10% haircut on **total premium** and $0.65 fee. The same
selected contracts are assessed with 0/5/20% haircuts. The ITM call haircut also
reduces intrinsic value and can erase time-value reward: cost-dependent findings
are explicitly flagged. These assumptions are not measured historical spreads.

## Characteristics and evidence

Decision features include intrinsic/time value, premium/time-value yield,
breakeven protection, capped reward, transactions and volume. Stock conditions
use contiguous completed sessions for 20-return annualized realized volatility
and 5-/20-session returns. Missing warmup produces unavailable features, not
backfilled values. Interim stock lows relative to entry and breakeven are path
diagnostics, not option-position mark-to-market drawdowns.

Development outcomes mature strictly before September 17, 2025. Origins on or
after that date form the 2025 holdout; crossing development outcomes are purged.
The verified CIFR/IREN/WULF 2026 supplement is separate confirmation through
September 28. NBIS has no verified 2026 stock panel. Other eligible dates remain
descriptive. The source vintage and original coverage limits are documented in
[the historical research methodology](historical-options-research.md).

Provisional choices need twenty scored development origin dates, four expiries,
and positive reward/day. Pareto examples maximize reward/day and minimize
worst-5% loss: conservative takes the smallest tail loss, higher-return takes
highest reward, balanced takes the lower middle point ordered by reward.
Equivalent choices may share a rule. No supported positive choice produces no
supported sale recommendation. This is not a guaranteed profit claim.

At most one condition can be attached: development volatility tercile,
positive/nonpositive 20-session return, or at least five transactions and twenty
contracts of volume. Volatility thresholds weight unique development origins.
A condition must improve development reward without worsening tail loss, or
improve tail loss without reducing reward, on the matched parent opportunities.
It also needs twenty active origin dates and four expiries. Failed conditions
retain stock return for calls or cash return for puts; unknown features are
excluded from both sides of the comparison. Traded-leg and complete-opportunity
results are separate. Holdout data never retune any choice or condition.

Worst-5% expected shortfall allocates fractional probability mass at the tail
boundary, preserving payoff atoms. Complete calendar-date moving blocks of 26
exchange sessions use 2,000 draws and seed 1729; every rule/ticker shares the
calendar sampling stream. Intervals require twenty scored dates and eight full
nonoverlapping calendar blocks containing scored support. Otherwise estimates
remain descriptive and underpowered. All comparisons are exploratory. Different
rule availability and expiry weekdays are disclosed in matched-date tables,
including paired reward and tail differences with calendar-block uncertainty;
raw frontiers do not establish a universally superior rule.
Missing-entry rates count unknown entry observations separately from known
opening-moneyness cancellations. Total no-entry rates and both components remain
available in the machine-readable summaries.

## Artifacts and limitations

- `candidates.parquet`: every eligible observed contract and its failure status.
- `opportunities.parquet`: one fixed selection per ticker/origin/depth/expiry rule.
- `cost_outcomes.parquet`: identical selections under each premium-cost assumption.
- `summary.json`: all descriptive rules, intersections, conditions and frozen choices.
- `checklist.md` and `report.md`: practical criteria, risk/reward, coverage and evidence.
- `manifest.json` and `resource.json`: analytical hashes, code/environment provenance,
  runtime and peak memory. Resource measurements do not enter analytical hashes.

All-contract descriptive means weight shared origin/expiry cells; selected-rule
metrics weight origins. More strikes cannot manufacture independent support.
Prices are current-vintage trade-bar proxies with unknown individual trade times,
not executable or synchronized quotes. Expiry ITM is a terminal exercise
scenario; actual assignment, early exercise and buying-power release are unknown.
Historical IV/delta, spreads, open interest, earnings events, taxes and account
history are not modeled. Findings apply to these four stocks and this vintage.
