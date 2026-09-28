# HyperOptions

HyperOptions is a local workstation for exploring covered calls and cash-secured puts on **Nasdaq-listed** stocks and watching selected option contracts. It is not a brokerage, does not store account or holdings data, and is not financial advice. The option chain and watchlist lead with a dated historical predictive forecast and show market-implied odds separately when public quotes support them.

The desktop UI keeps ticker setup, quote context, strategy, moneyness, sizing, and refresh in a sticky left sidebar. Below 64rem the same controls live in an accessible Settings drawer beneath a compact ticker/price/status summary. The results canvas groups provider-returned contracts by expiration with exact strikes high to low.

The app binds to loopback only (`127.0.0.1`). Market data is fetched unofficially from public Nasdaq JSON endpoints for personal local use and is not affiliated with Nasdaq.

The React + Vite frontend is Tailwind CSS v4 + shadcn/ui on Base UI primitives, with `@/` imports, self-hosted Geist Sans/Mono (no CDN fonts), and light/dark themes. Theme and table density persist in `localStorage` only — they are not URL params. `npm audit` is a completion gate; keep new frontend dependencies exact-pinned.

The React browser client talks only to local FastAPI. Python fetches public Nasdaq quotes and option chains, a dated U.S. Treasury yield curve, and Yahoo Finance data when needed. Computed contracts are reused while their inputs are unchanged. The Nasdaq-listed universe starts loading when the backend starts and refreshes daily. Ticker search, option-chain requests, and new watches fail closed when it cannot be loaded; expiry-result checks for saved watches can still run. See [third-party notices](THIRD_PARTY_NOTICES.md) for data-use and dependency terms.

## Run

From the directory containing `backend/`, `frontend/`, and `scripts/` (run `cd hyperoptions` first if your shell is in the enclosing `HyperOptions` folder). Use Node.js 22, as CI does; Node.js 23 is not supported by Vitest.

```bash
./scripts/dev
```

`./scripts/dev` requires a clean `main` checkout with `origin` pointing to
`hypertrial/hyperoptions`. It checks `origin/main` before installing locked
dependencies, fast-forwards a clean checkout, and then starts both services.
If the remote cannot be reached, it warns that the version is unverified and
starts the clean local checkout. A feature branch, local changes, or divergent
history stops startup with a repair message. While running, the UI checks for
a newer remote revision; restart `./scripts/dev` to apply it. Open
[http://127.0.0.1:5173](http://127.0.0.1:5173). Ctrl+C stops both processes.

For separate terminals:

```bash
cd backend
uv sync --frozen --group dev --group research
uv run --group research uvicorn options_api.main:app --reload --no-access-log --host 127.0.0.1 --port 8000
```

```bash
cd frontend
npm ci
npm run generate:api
npm run dev
```

## Page

The first load is IREN covered-call ITM when the URL has no `t` parameter. Ticker, strategy, moneyness, and visible columns persist in `t`, `side`, `m`, and `cols`. An explicit `cols` URL is honored on load; deliberately switching strategy returns to that strategy's focused defaults. Refresh reloads the selected ticker and strategy. If a manual refresh fails, the last successful chain stays visible with a stale-data warning and retry. If the ticker universe is temporarily unavailable, ticker selection stays disabled until a successful **Retry ticker list** request. Ticker and strategy changes still clear mismatched data while loading.

The workstation navigation shows the option chain at `/` and selected contracts at `/watchlist`. Former `/research/*` browser links redirect to the watchlist; an unknown path shows links back to both pages. There is no manual Research dashboard, data update action, or full-catalog backtest API. The durable background queue handles expiry-close results. A restart marks interrupted jobs as failed. The queue admits one running job and up to four pending jobs, and a second app instance cannot use the same local data directory.

The focused call defaults are **Strike, Bid, Spread %, OI, Premium, Called P&L, APR (net), and Drop (BE)**. Put defaults are **Strike, Bid, Spread %, OI, Premium, Breakeven, APR (net), and Cushion (BE)**. The grouped Columns picker retains Ask, dollar spread, volume, capital/outlay metrics, alternate return and risk metrics, historical lows, and Greeks, with Reset to default and Show all actions.

`current` is the valid stock bid, or the chain last trade when bid is missing. Calls are ITM when `strike < current`; puts are ITM when `strike > current`. Equality is ATM and is excluded from both the ITM and OTM chain filters. The same `current` is used for drop/cushion-to-strike. The public API sends these as integers: `*_cents` for currency, `*_pct_tenths` for one-decimal percentages, and `*_e4` for Greeks (×10,000). Backend calculations use `Decimal` and quantize with `ROUND_HALF_UP` at the response boundary. The browser compares filters to those scaled integers through exact decimal tokens, not by formatting then parsing.

One-contract, zero-commission buy-write (covered calls):

- `stock cost = 100 × current`
- `premium = 100 × call bid`
- `net outlay = stock cost − premium`
- `effective cost = current − call bid`
- `called P&L = premium − 100 × (current − strike) = 100 × strike − net outlay`
- `called P&L / sh = strike + call bid − current`
- `APR (net) = called P&L / net outlay × 365 / DTE`
- `APR (stock) = called P&L / stock cost × 365 / DTE`
- `drop to strike = (current − strike) / current`
- `drop to breakeven = (current − effective cost) / current`

One-contract cash-secured put:

- `premium = 100 × put bid`
- `collateral = 100 × strike`
- `net collateral = collateral − premium`
- `breakeven = strike − put bid`
- `APR (collateral) = premium / collateral × 365 / DTE`
- `APR (net) = premium / net collateral × 365 / DTE`
- `cushion to strike = (current − strike) / current`
- `cushion to breakeven = (current − breakeven) / current`

The Contracts field is a whole number of 100-share lots. Covered-call stock cost, premium, net outlay, and called P&L are multiplied by that count. CSP premium, collateral, and net collateral are multiplied. Bid, ask, volume, APRs, drops/cushions, effective cost, called P&L / sh, breakeven, Greeks, and low comparisons stay one-contract. Leave Contracts empty to keep the one-contract view. `0`, negatives, decimals, values too large for exact integer calculations, or other invalid text show validation and fall back to one contract.

Filters start closed. Optional minimums for Called P&L (calls) or Premium (puts), **APR net**, and **drop/cushion to breakeven**, plus an inclusive Min DTE and Max DTE range, combine with AND. Values compare directly with the API's scaled integers. Enter percent points (`40` means 40%). Invalid tokens show inline validation and are otherwise ignored; null metrics fail only their active filter. An inverted DTE range matches nothing and asks you to fix the range. Valid filters count as applied; invalid entries show a separate **Fix** count. Each entry has its own clearable chip, and Clear all restores the full chain and does not clear Contracts.

Calls or puts with side open interest below 5, or with missing OI, are omitted.

Greeks are a European, no-dividend Black-Scholes approximation of American equity options; early exercise and dividends are not modeled. Implied volatility uses a valid bid/ask midpoint within no-arbitrage bounds. The calculation uses the coherent quote session and time remaining until the listed expiry session close, with the same dated rate as the market-odds snapshot. Theta is per share per calendar day. Vega is per 1 volatility point. Rho is per 1 percentage-point change in the risk-free rate. Greek columns stay hidden until enabled.

7/30/90/365-day lows use completed Nasdaq daily lows, `today-N <= date < today`. Each signed column is `(strike − period low) / period low` for both strategies. Missing quotes or windows render as em dash. Hover a column name for its definition. Subtle per-expiration heat is limited to call Called P&L/APR net/Drop (BE) or put Premium/APR net/Cushion (BE); price and liquidity remain neutral, so color is never required to interpret a value. Each desktop and tablet table scrolls horizontally when every column cannot fit. Strike stays visible on the left and Copy on the right. Copy includes every selected column plus ticker, expiration, DTE, current price, and contract-count context. If Nasdaq reaches its 5,000-row cap, the backend attempts a complete Yahoo chain replacement and labels any remaining coverage gap.

Every matching expiration has a collapsible header with date, DTE, and row count. Only the nearest expiration opens after ticker, strategy, or moneyness changes. Expand all and Collapse all are available; filtering, sorting, columns, and density preserve session-local expansion choices. Collapsed bodies are unmounted. When a table overflows horizontally, it shows a scroll cue and can receive keyboard focus; narrow phones use expandable contract cards.

Large filtered result sets render progressively across expanded groups only: all expiration headers remain discoverable, while the first 250 expanded rows mount. Accessible **Show 250 more** (or the remaining count, when fewer than 250 rows are left) and **Show all** controls reveal the rest. Heat ranges still use the complete filtered expiration. The reveal limit resets when the chain identity or filters change.

Below 40rem, the table becomes compact disclosure rows. Each summary shows up to four selected priority metrics—Strike, Bid, APR net, and breakeven protection, with selected-order fallback. Opening a row shows every remaining selected metric and Copy. Sorting remains available above the mobile rows.

A ticker with no options shows `Options are not available for {ticker}`. A priced ticker with no rows after the moneyness filter shows `No {ITM|OTM} {calls|puts} for {ticker}`.

## Selected-contract watchlist

Use **Watch** on a desktop or mobile chain row, then open `/watchlist`. Watches track contracts, not positions or trades; the app does not store holdings, premiums, or assignment decisions. Any supported Nasdaq-listed ticker may be watched. The server checks each submitted watch key against the current chain and Nasdaq universe and deduplicates the exact ticker, option root, call/put side, expiration, and strike. Only ordinary contracts whose parsed root and terms match the chain row are eligible. Adjusted or ambiguous series have a disabled Watch control with a reason; saved watches show **“Assuming standard 100-share terms.”**

Before expiry, the chain and watchlist lead with a dated **stock forecast** showing ITM and OTM odds for the real-world expiry-session close. The event is `close > strike` for a call and `close < strike` for a put. Equality remains a separate outcome in the model and API, so the two displayed percentages can total less than 100%. Completed Yahoo `Close` history gives the dated physical forecast. The default is a zero-drift lognormal distribution with 60-session EWMA volatility. Compare models also shows volatility-scaled empirical, Student-t EWMA, GJR-GARCH Student-t, daily-OHLC range/HAR proxy, skewed-t EWMA, EGARCH skewed-t, two-regime switching variance, pooled NGBoost, earnings-jump, IV-informed, and intraday results. A user-selected physical model drives the compact odds and hypothetical risk; no model is automatically chosen from past scores. The new historical methods support 1–25 sessions, while EWMA supports up to one year. The latest completed split-safe bar and verified cache are required. Rights-dependent methods remain visibly unavailable until their input provenance qualifies. Each result shows its availability, input and fit diagnostics, and separately measured accuracy when matured outcomes exist. The comparison reports independent empirical history blocks, a maximum 95% Monte Carlo error for simulated odds, and the spread between comparable completed-close methods. Those describe sampling or method sensitivity, not predictive accuracy. Missing evidence is N/A, not forecast confidence.

Student-t EWMA bounds each simulated terminal price to 1%–100× the starting close to prevent unbounded price tails from dominating full-distribution scores. This changes its model version, so earlier Student-t evidence is not treated as current-version accuracy.

**Market-implied odds** are displayed separately as risk-neutral option-price context, not as a substitute for the physical forecast. The `regimelib` estimator fits one two-state distribution to validated call quotes using the Treasury rate for each expiry, then prices a cash digital. A separate per-expiry decreasing, convex call-price curve is also shown when its quote and one-tick stability checks pass. SSVI is listed separately but remains unavailable until coherent, rights-cleared multi-expiry quotes and defensible American-exercise and dividend treatment exist. Wide bounds remain visible; quote tightness and held-out quote fit describe market-input robustness, not realized forecast accuracy. Sparse or contradictory strips cannot provide a precise market probability. American exercise, dividends, and missing individual quote timestamps still limit interpretation. The physical and risk-neutral probabilities are never combined into a recommendation score.

Visible chain pages refresh odds about every five minutes during the regular trading session; a visible watchlist polls for updated cached odds. If a later watchlist poll fails, the last loaded list stays visible with a dated warning and Retry action. After hours, quote-implied valuation uses the latest completed session's official daily close and its dated rate. A watched contract's last valid market value from the latest completed session remains visible as dated context across refreshes and restarts, then disappears when a newer session completes. The compact stock summary shows its quote time, and market-session labels describe the source state at fetch. The fetch time is a snapshot time, not a claim about each option's quote timestamp; fetched and retrieved times include the browser's local timezone, while market quote time is labeled ET. Legacy strategy forecast snapshots remain in the local database for historical continuity but are never served as current odds. **Check expiry results** requests a separate background close-based outcome update.

The chain and watchlist can also show **hypothetical one-contract hold-to-expiry risk** from the selected physical distribution and coherent, dated entry quotes. A covered call assumes buying 100 shares at the displayed stock ask and selling one call at the displayed bid: `P&L = 100 × (min(expiry close, strike) − stock entry + call bid)`. A cash-secured put assumes selling one put at its displayed bid: `P&L = 100 × (put bid − max(strike − expiry close, 0))`. The quoted entry cost does not silently reanchor a completed-close forecast. The intraday method separately estimates overnight and remaining regular-session variance from Open/Close history and reanchors both odds and risk to a validated live underlying quote. Expected P&L, loss probability, and fifth-percentile P&L are distribution estimates, not realized returns. They omit fees, dividends, early assignment, and execution effects. When coherent quotes are missing or stale, these metrics remain unavailable; after hours, a covered-call purchase price cannot be verified and its metrics are withheld. Watches still contain no actual trades, holdings, or premiums.

After the expiry session completes, the separate **Expiry · close-based result** compares the exact strike with Yahoo's dated daily `Close` from the completed trading session on or before expiry. A call is ITM only when `close > strike`; a put is ITM only when `close < strike`; equality is ATM, and the opposite side is OTM. The card records the source, session date, retrieved time, close, and assumed contract terms. The request sets `auto_adjust=False`, but [Yahoo describes `Close` as split-adjusted](https://ca.finance.yahoo.com/quote/ADI/history/), so a result is always labeled **provisional and indicative**. Missing closes remain pending and are retried on the next app start or an explicit refresh; detected splits after the watch's latest completed session or uncertain contract terms withhold the result with a reason. A sourced result is retained with a warning if a later recheck becomes unsafe. This is not an [OCC exercise or assignment determination](https://infomemo.theocc.com/infomemos?number=39744).

## API

`GET /api/health`, `GET /api/tickers?q=&limit=`, `GET /api/covered-calls/{ticker}?moneyness=`, and `GET /api/cash-secured-puts/{ticker}?moneyness=` serve the chain. Eligible chain rows include a backend-generated `watch_key`; ineligible rows include `watchability_reason`. The watchlist uses `POST /api/watchlist` with that key, `GET /api/watchlist`, `DELETE /api/watchlist/{id}`, and `POST /api/watchlist/refresh`; job progress is available from `GET /api/jobs/{id}`. The manual `/api/research/*` routes have been removed. Tickers must match `^[A-Z]{1,5}$` and belong to the Nasdaq-listed universe. The universe fails closed: if it cannot be loaded, ticker search and chain routes return 503. Unknown symbols return 404. Host validation admits loopback hosts only, including IPv6 `::1`. CORS allows the local Vite origin. API writes require that origin and JSON, and write bodies are capped at 16 KiB.

`GET /api/version?frontend_sha=` reports the frozen running revision, branch,
remote revision or offline status, check time, and optional frontend/backend
revision match. Its remote check is cached for 60 seconds and never changes
the running checkout.

The checked-in `frontend/openapi.json` is generated from FastAPI. `npm run generate:api` regenerates TypeScript types and Zod page validators under `frontend/src/generated/`. `npm run check:api` fails if that output drifts.

Nasdaq fetches use a bounded policy: the required option chain has two attempts, a 15s read timeout, and a 22s overall deadline. Optional stock info and history use a shorter 8s deadline so a degraded page does not wait through two full chain timeouts. HTTP 429 is not retried; numeric `Retry-After` is forwarded. The ticker screener uses the same 15s read timeout as the chain.

## Data files

Watch jobs, watched contracts, close-based observations, append-only forecast issuances and label revisions, market-curve shadow reports, the latest-session watched market value, and legacy forecast snapshots are stored in separate tables of `.local/research/results.duckdb`. Verified Yahoo history and manifests live under `.local/research/`; immutable 50-ticker audit inputs are explicitly labeled current-vintage retrospective snapshots. Existing legacy files and Research tables remain untouched. Set `STOCKSWEEPER_DATA_DIR` to use another local data directory. These files are excluded from Git. Any leftover `.local/options.duckdb` or `.local/options.sqlite3` file is left untouched.

## Forecast evaluation

From `backend/`, `uv run --group research python scripts/evaluate_predictive.py IREN --refresh` refreshes public Yahoo closes and prints a retrospective rolling-origin report for the older per-origin EWMA/empirical selection policy, alongside its EWMA baseline comparator. That policy is research-only and does not choose the app's default forecast. Without `--refresh`, the command reads only the verified cache; `--as-of YYYY-MM-DD` evaluates a past completed session. To freeze the deterministic 50-stock convenience sample from eligible, verified cached Nasdaq stocks, run `uv run --group research python scripts/freeze_audit_cohort.py`. Then screen a challenger with `uv run --group research python scripts/evaluate_predictive.py --replay-cohort --candidate student_t_ewma --period screen`. These immutable snapshots contain current-vintage history, so replay is retrospective screening, never as-issued evidence.

Live forecast attempts for each method are recorded in an append-only DuckDB ledger, including unavailable attempts. Expected capture windows are recorded separately so missed windows are visible. The app's per-model evidence uses only issuances carrying the currently displayed model version. Its candidate coverage is conditional on recorded current-version candidate cells; cells without EWMA count as failures. Older generic-version attempts and missed origins remain outside this rate and are not silently counted as current-version failures. Once exact expiry-session closes have matured and passed the Nasdaq/Yahoo cross-check, `uv run --group research python scripts/evaluate_predictive.py --ledger-contest --candidate student_t_ewma --period holdout --holdout-start YYYY-MM-DD` reports paired as-issued Brier score, log loss, full-distribution score, calibration, availability, rejection reasons, and latency by horizon, moneyness, and volatility regime. Contract sides and strikes sharing one stock close are averaged into one ticker-origin-horizon unit; overlapping intervals are purged and calendar-date blocks are bootstrapped with all tickers together. The comparison dialog shows a calendar-block interval adjusted for the ten predeclared physical-model comparisons **within each horizon band** when independent support exists; comparisons across bands remain exploratory. These reports measure accuracy on matched observations and never change the selected model. An older `forecast_champions.json` file, if present, is left untouched and ignored.

The [source-rights audit](docs/source-rights.md) explains why this release does not expand Yahoo downloads or store Nasdaq option quotes for training. Pooled NGBoost also needs its exact trained-artifact hash recorded with each issued forecast before live numbers can be shown. Earnings-jump, IV-informed physical, and SSVI market estimates require qualified inputs; their unavailable reasons remain visible in the comparison dialog.

`uv run --group research python scripts/evaluate_intraday_open.py IREN` screens the intraday method on historical Opens; prospectively timestamped 10:00, 13:00, and 15:30 ET snapshots are recorded for separate accuracy measurement. `uv run --group research python scripts/evaluate_intraday_prospective.py` scores matched, matured as-issued snapshots against both the dated-close forecast and simple quote reanchor, with window coverage and latency. `uv run --group research python scripts/evaluate_market_curve.py` reports market-curve quote fit, coverage, rejections, and latency. Market-implied odds are judged on quote consistency rather than realized-outcome Brier score. These reports describe stock-close forecasts and option-quote fits; they do not claim retrospective option-trading performance.

## Tests

```bash
(cd backend && uv run --group research pytest -q)
(cd frontend && npm test)
(cd frontend && npx --no-install playwright install chromium)
(cd frontend && npx --no-install playwright test)
```

Playwright starts a mocked FastAPI server and Vite, then proves the `/api` proxy and that the browser never calls Nasdaq. Install the pinned Chromium before the first local run; `npm install` does not download it. Use the project-pinned Playwright command; do not run a global `npx playwright`.

## Verification gates

```bash
scripts/verify-fast
scripts/verify
```

`scripts/verify-fast` is browser-free: documentation, shell syntax, backend tests, and frontend unit tests.

`scripts/verify` is the completion gate: whitespace checks, backend Ruff and tests, frontend npm audit, tests, lint, generated-contract cleanliness, production build, the pinned Chromium install, and Playwright. `scripts/check-wheel` builds the backend wheel and checks bundled TOML/SQL resources with the project's Python. CI installs frozen dependencies and the pinned Chromium revision, then runs both scripts.
