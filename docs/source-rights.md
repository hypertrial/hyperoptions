# Forecast input rights and provenance

Checked 2026-09-28. This is an engineering gate, not a license determination.

| Source | What the public source provides | Decision for this release |
| --- | --- | --- |
| [Nasdaq website terms](https://www.nasdaq.com/legal) | Section 7 requires express written permission to capture or extract site content for machine learning or data-analysis software. | Do not create a Nasdaq quote archive or train models on captured site quotes. Existing live display behavior is outside this expansion. |
| [Yahoo terms](https://legal.yahoo.com/ca/en/yahoo/terms/otos/) | Automated collection requires prior permission; a competing database or archive is restricted. [Yahoo Finance help](https://in.help.yahoo.com/kb/SLN2311.html) also says download rights vary by instrument. | Do not expand automated history downloads for model training. The new vintage helper only freezes already-local verified caches; it never fetches. Before a production-scale backfill or redistribution, obtain a suitable data license or substitute a rights-cleared source. |
| [Cboe historical archive](https://www.cboe.com/us/options/market_statistics/historical_data/) | Free historical option **volume**; bid/ask history is a separate [DataShop product](https://datashop.cboe.com/option-eod-summary). | Cannot support IV-informed training or SSVI quote backtesting. No new option-quote capture. |
| [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) | Public filing history and documents, not a normalized forward earnings calendar. [SEC fair-access guidance](https://www.sec.gov/about/webmaster-frequently-asked-questions) allows declared scripted access at no more than 10 requests/s. | Capture only explicit *future* earnings schedule announcements in 8-K exhibits. Preserve accession, acceptance, retrieval and event times, source hash and revisions. An actual earnings filing alone never establishes prior knowledge of its date. |

The `forecast/training/cohort.json` artifact is a hash-verified convenience sample frozen from local prices. It is separate from the 50-ticker audit cohort and carries `immutable_current_vintage_training` provenance. Replaying past dates from it is retrospective current-vintage screening, never an as-issued historical forecast. A missing or changed member fails closed. The helper is deliberately offline; its presence does not grant rights to fetch or retain new vendor data.

Yahoo-derived cohort manifests carry `training_rights_unverified`. The normal cohort loader refuses to supply frames to pooled-model training while that status remains. An explicit inspection-only read can verify integrity; it does not qualify the data for model training. A separately provisioned, rights-cleared cohort can use `approved_for_training` only when its cohort and every immutable vintage carry the same explicit source name and license reference, each bound to the price data by SHA-256 manifest hashes. This metadata records an operator's source qualification; it is not itself a license grant.

Live pooled forecasts also remain unavailable for a qualified artifact until issuance and evidence records include that artifact's SHA-256. Otherwise a later retraining run could be scored as if it were the same model input.

For deliberate SEC capture, identify the issuer's CIK from SEC's [ticker/CIK mapping](https://www.sec.gov/files/company_tickers.json), then run from `backend/`:

```bash
HYPEROPTIONS_SEC_USER_AGENT='HyperOptions YourContact@example.com' \
  uv run --frozen python scripts/capture_sec_events.py AAPL 320193
```

The command inspects at most five recent 8-Ks for that exact ticker/CIK and, for each filing, at most one unambiguous HTML/text EX-99.1 attachment. It does not run at app startup, scrape a calendar, or infer an event from a filing date. Schedule it only with a real declared SEC contact and a deliberate ticker list; repeated runs are idempotent. An earnings-jump model requires an explicit prior future schedule **and** a separately dated actual results release matching the exact event date. A schedule alone is insufficient. Even both dated filings do not establish the actual release **clock time**; a numeric jump model remains unavailable until that time and enough verified, split-safe event-return outcomes support the overnight/intraday alignment.

Expected intraday capture windows are recorded independently of successful forecast issuances, so missed windows remain in the denominator. Already-recorded expectations continue to resolve after watch deletion. A watch deleted while the app was offline, before its expectation was recorded, cannot be reconstructed from the current watch table; such gaps must be reported separately rather than counted as successful coverage. Read-only capture counts are limited to a 35-day interval and separate from matched forecast-score coverage.
