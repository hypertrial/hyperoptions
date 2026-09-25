# HyperOptions

HyperOptions is a local, MIT-licensed workstation for selling covered calls and cash-secured puts on **Nasdaq-listed** stocks. It is not a brokerage, does not store account or holdings data, and is not financial advice.

The desktop UI keeps ticker setup, quote context, strategy, moneyness, sizing, and refresh in a sticky left sidebar. Below 64rem the same controls live in an accessible Settings drawer beneath a compact ticker/price/status summary. The results canvas groups provider-returned contracts by expiration with strikes high to low.

The app binds to loopback only (`127.0.0.1`). Market data is fetched unofficially from public Nasdaq JSON endpoints for personal local use and is not affiliated with Nasdaq.

The React + Vite frontend is Tailwind CSS v4 + shadcn/ui on Base UI primitives, with `@/` imports, self-hosted Geist Sans/Mono (no CDN fonts), and light/dark themes. Theme and table density persist in `localStorage` only — they are not URL params. `npm audit` is a completion gate; keep new frontend dependencies exact-pinned.

The React browser client talks only to local FastAPI. Python is the only Nasdaq caller and caches option chains and stock quotes for 30 seconds and daily OHLCV for 24 hours. Computed contracts are reused while their inputs are unchanged. The Nasdaq-listed universe starts loading when the backend starts, refreshes daily, and fails closed when it cannot be loaded.

## Run

From the repository root:

```bash
./scripts/dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). Ctrl+C stops both processes.

For separate terminals:

```bash
cd backend
uv sync --group dev
uv run uvicorn options_api.main:app --reload --no-access-log --host 127.0.0.1 --port 8000
```

```bash
cd frontend
npm install
npm run generate:api
npm run dev
```

## Page

The first load is IREN covered-call ITM when the URL has no `t` parameter. Ticker, strategy, moneyness, and visible columns persist in `t`, `side`, `m`, and `cols`. An explicit `cols` URL is honored on load; deliberately switching strategy returns to that strategy's focused defaults. Refresh reloads the selected ticker and strategy. If a manual refresh fails, the last successful chain stays visible with a stale-data warning and retry. Ticker and strategy changes still clear mismatched data while loading.

The focused call defaults are **Strike, Bid, Spread %, OI, Premium, Called P&L, APR (net), and Drop (BE)**. Put defaults are **Strike, Bid, Spread %, OI, Premium, Breakeven, APR (net), and Cushion (BE)**. The grouped Columns picker retains Ask, dollar spread, volume, capital/outlay metrics, alternate return and risk metrics, historical lows, and Greeks, with Reset to default and Show all actions.

`current` is the valid stock bid, or the chain last trade when bid is missing. Calls are ITM when `strike < current`. Puts are ITM when `strike > current`. Equality is not ITM. The same `current` is used for drop/cushion-to-strike. The public API sends these as integers: `*_cents` for currency, `*_pct_tenths` for one-decimal percentages, and `*_e4` for Greeks (×10,000). Backend calculations use `Decimal` and quantize with `ROUND_HALF_UP` at the response boundary. The browser compares filters to those scaled integers through exact decimal tokens, not by formatting then parsing.

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

Filters start closed. Optional minimums for Called P&L (calls) or Premium (puts), **APR net**, and **drop/cushion to breakeven**, plus an inclusive Min DTE and Max DTE range, combine with AND. Values compare directly with the API's scaled integers. Enter percent points (`40` means 40%). Invalid tokens show inline validation and are otherwise ignored; null metrics fail only their active filter. An inverted DTE range matches nothing. Active filters appear as individually removable chips, and Clear all restores the full chain and does not clear Contracts.

Calls or puts with side open interest below 5, or with missing OI, are omitted.

Greeks are a European, no-dividend Black-Scholes approximation of American equity options. Implied volatility uses the sell bid first, then the mid if the bid is outside no-arbitrage bounds. Theta is per share per calendar day. Vega is per 1 volatility point. Rho is per 1 percentage-point change in the risk-free rate. The rate comes from `OPTIONS_RISK_FREE_RATE` (default `0.04`) and is never a query parameter. Greek columns stay hidden until enabled.

7/30/90/365-day lows use completed Nasdaq daily lows, `today-N <= date < today`. Each signed column is `(strike − period low) / period low` for both strategies. Missing quotes or windows render as em dash. Hover a column name for its definition. Subtle per-expiration heat is limited to call Called P&L/APR net/Drop (BE) or put Premium/APR net/Cushion (BE); price and liquidity remain neutral, so color is never required to interpret a value. Each desktop and tablet table scrolls horizontally when every column cannot fit. Strike stays visible on the left and Copy on the right. Copy includes every selected column plus ticker, expiration, DTE, current price, and contract-count context. Nasdaq's 5,000-row cap is warned when the chain is truncated.

Every matching expiration has a collapsible header with date, DTE, and row count. Only the nearest expiration opens after ticker, strategy, or moneyness changes. Expand all and Collapse all are available; filtering, sorting, columns, and density preserve session-local expansion choices. Collapsed bodies are unmounted.

Large filtered result sets render progressively across expanded groups only: all expiration headers remain discoverable, while the first 250 expanded rows mount. Accessible **Show 250 more** (or the remaining count, when fewer than 250 rows are left) and **Show all** controls reveal the rest. Heat ranges still use the complete filtered expiration. The reveal limit resets when the chain identity or filters change.

Below 40rem, the table becomes compact disclosure rows. Each summary shows up to four selected priority metrics—Strike, Bid, APR net, and breakeven protection, with selected-order fallback. Opening a row shows every remaining selected metric and Copy. Sorting remains available above the mobile rows.

A ticker with no options shows `Options are not available for {ticker}`. A priced ticker with no rows after the moneyness filter shows `No {ITM|OTM} {calls|puts} for {ticker}`.

## API

`GET /api/health`, `GET /api/tickers?q=&limit=`, `GET /api/covered-calls/{ticker}?moneyness=`, and `GET /api/cash-secured-puts/{ticker}?moneyness=` are the public endpoints. Tickers must match `^[A-Z]{1,5}$` and belong to the Nasdaq-listed universe. The universe fails closed: if it cannot be loaded, ticker search and chain routes return 503. Unknown symbols return 404. Host validation admits loopback hosts only, including IPv6 `::1`. CORS allows the local Vite origin.

The checked-in `frontend/openapi.json` is generated from FastAPI. `npm run generate:api` regenerates TypeScript types and Zod page validators under `frontend/src/generated/`. `npm run check:api` fails if that output drifts.

Nasdaq fetches use a bounded policy: the required option chain has two attempts, a 15s read timeout, and a 22s overall deadline. Optional stock info and history use a shorter 8s deadline so a degraded page does not wait through two full chain timeouts. HTTP 429 is not retried; numeric `Retry-After` is forwarded. The ticker screener uses the same 15s read timeout as the chain.

## Data files

The app no longer reads or writes observation databases. Any leftover `.local/options.duckdb` or `.local/options.sqlite3` file is left untouched.

## Tests

```bash
(cd backend && uv run pytest -q)
(cd frontend && npm test)
(cd frontend && npx --no-install playwright test)
```

Playwright starts a mocked FastAPI server and Vite, then proves the `/api` proxy and that the browser never calls Nasdaq. Use the project-pinned Playwright command; do not run a global `npx playwright`.

## Verification gates

```bash
scripts/verify-fast
scripts/verify
```

`scripts/verify-fast` is browser-free: documentation, shell syntax, backend tests, and frontend unit tests.

`scripts/verify` is the completion gate: whitespace checks, backend ruff, backend tests, frontend npm audit, frontend tests, lint, generated-contract cleanliness, production build, and Playwright. CI installs frozen dependencies and the pinned Chromium revision, then runs `scripts/verify` once.
