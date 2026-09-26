# Project agent notes

HyperOptions is a public, MIT-licensed local workstation for covered calls and cash-secured puts on Nasdaq-listed stocks. The GitHub repository is `hypertrial/hyperoptions`. The app is loopback-only; it is not a hosted brokerage product and stores no account or holdings data.

Stack: FastAPI (`backend/`) and React + TypeScript + Vite (`frontend/`). The frontend uses Tailwind CSS v4 (`@tailwindcss/vite`), shadcn/ui generated from Base UI (`--base base`), `@/` path aliases, and self-hosted Geist fonts. Theme (`light|dark|system`) and table density persist in `localStorage`. The browser must never query Nasdaq; all Nasdaq traffic goes through Python. Do not add CDN fonts or unpinned `@latest` UI deps — `npm audit` is part of `scripts/verify`.

Use the `hyperoptions-engineering` workspace from `.pad.toml`. Follow `AGENTS.md` and the local `pad-engineering` skill. This GitHub repository is public: keep Pad bodies, comments, bootstrap JSON, emails, credentials, and backups out of git.

## Public contract

Financial fields on `GET /api/covered-calls/{ticker}` and `GET /api/cash-secured-puts/{ticker}` are scaled integers: `*_cents` for currency, `*_pct_tenths` for one-decimal percentages, and `*_e4` for Greeks (×10,000). Internal calculations use `Decimal` and quantize with `ROUND_HALF_UP`. Frontend contract-count and filter inputs are exact tokens compared to those integers; do not format-then-parse.

Ticker membership is the Nasdaq-listed universe (`^[A-Z]{1,5}$`), not a hardcoded allowlist. Fail closed with 503 when the universe is unavailable. `npm run generate:api` regenerates OpenAPI types and Zod validators under `frontend/src/generated/`. `npm run check:api` must stay clean.

Greeks are a European, no-dividend Black-Scholes approximation of American equity options. IV uses a coherent bid/ask midpoint inside no-arbitrage bounds. Live Greeks use the dated Treasury curve rate and the time from the coherent quote to the expiry trading session close; each contract exposes its rate and session date. The `OPTIONS_RISK_FREE_RATE` default remains only for direct offline chain calculations and is never a query parameter.

## Verification

- Fast: `scripts/verify-fast` (backend pytest + frontend `npm test`, no Playwright)
- Completion: `scripts/verify` (diff/docs/shell, backend ruff, backend tests, frontend npm audit, frontend tests, lint, generated contract, production build, Playwright)

CI installs frozen uv/npm dependencies and the pinned Playwright Chromium revision, then invokes `scripts/verify` once.

## Invariants

Never commit Pad bodies, credentials, backups, or brokerage/account data. Do not push directly to `main`. Bind FastAPI and Vite to loopback only. Host validation and CORS admit localhost/`127.0.0.1`/`::1` only. The frontend must not call `api.nasdaq.com`. Large chains render progressively (initial 250 rows, then show more / show all). Nasdaq retries are bounded by `FetchPolicy`; do not retry 429 or 4xx/5xx.
