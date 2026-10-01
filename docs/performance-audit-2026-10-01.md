# Offline audit-path measurements

These opt-in measurements cover the five unconfirmed performance leads from the
audit at `ccc8912`. They measure the implementation on 2026-10-01 using synthetic
inputs only: the backend working tree based on `1b10704` with the evidence changes
present, and the finalized frontend at `1e74893`. No vendor requests, downloaded
market history, private data, or new dependencies are involved. The results do
not justify additional production rewrites in this PR.

## Reproduction

From the repository root, with its installed Python 3.12 environment:

```sh
PYTHONPATH=backend/src backend/.venv/bin/python backend/scripts/benchmark_audit_paths.py > /tmp/hyperoptions-backend-benchmark.json
```

From `frontend/`, using Node 22 and the already-installed Playwright Chromium:

```sh
node scripts/benchmark-audit-paths.mjs > /tmp/hyperoptions-frontend-benchmark.json
```

The recorded frontend run used `/opt/homebrew/opt/node@22/bin/node`. The script
builds the normal application and a separate fixture harness in temporary
directories, serves them on `127.0.0.1` on an ephemeral port, mocks every API
response, blocks external browser requests, and removes the outputs when done.
It does not change `src/`, `dist/`, or generated API contracts. A restricted
runner must permit loopback listening and Chromium execution. Install the pinned
browser through the existing project workflow if it is absent.

Both entrypoints print JSON containing individual samples, operation counts,
runtime details, and limitations. Python also prints cumulative `cProfile`
summaries and peak `tracemalloc` bytes from separate additional samples.
Each measurement phase has a 60-second cap, two warm-ups, and seven measured
samples. Frontend pure derivation averages 20 invocations per sample; fixture
construction is outside the timer. The production build is one bounded sample,
not seven builds. No timing threshold is asserted in CI.

Recorded environment: Python 3.12.13, Node 22.23.2, headless Chromium
140.0.7339.186, macOS 27.0.1, arm64. Other implementation and test processes were
active; repeated local runs varied materially. Compare operation counts first.
These are diagnostic workstation measurements, not an idle-machine benchmark,
real-provider latency measurement, or a speedup guarantee.

## Complete-chain derivation and progressive rendering

The production fixture harness imports the existing `largeChainPage` fixture and
the real `deriveChainView`, columns, filters, scaling, sorting, and heatmap code.
Filters are open, sizing is one contract, sorting is descending strike, and the
reveal limit is 250. Counts are measured separately through column accessors.

| Input rows | Median per derivation (ms) | Min–max (ms) | Rows scanned | Accessor calls | Mounted rows |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 250 | 0.110 | 0.100–0.175 | 250 | 1,248 | 250 |
| 1,000 | 0.520 | 0.510–0.550 | 1,000 | 4,998 | 250 |
| 5,000 | 1.990 | 1.975–2.080 | 5,000 | 24,998 | 250 |

The entire chain is derived before the display limit. Copy and density state
changes still execute `ItmChain`, whose current render calls this derivation.
The production application was also exercised through actual copy and density
buttons:

| Input rows | Density median/min–max (ms) | Copy median/min–max (ms) |
| ---: | ---: | ---: |
| 250 | 277.379 / 266.583–289.180 | 133.204 / 116.607–134.091 |
| 1,000 | 250.004 / 249.865–254.282 | 116.651 / 116.613–116.741 |
| 5,000 | 233.145 / 216.599–233.504 | 116.676 / 116.489–116.869 |

Interaction times include Playwright dispatch and two animation frames; they
cannot be attributed to derivation alone. The isolated calculation stayed around
2 ms or less in this fixture run, while the mounted desktop table stayed at 250 rows.
Keep the current derivation and progressive rendering. Qualify memoization only
if a representative interaction profile shows material calculation CPU on
slower hardware or richer chains.

These are deterministic CPU-size fixtures, not plausible option chains. The
existing 5,000-row fixture includes nonpositive terminal strikes and simplified
financial relationships. It does not reproduce many expiry groups, every sort
mode, model evidence size, or a licensed live snapshot.

## Verified provenance filesystem checks

A temporary price cache contains one synthetic completed Close and a real cache
manifest written by `ForecastPriceStore` with a synthetic provider. The first
lookup verifies its input hash through the real ledger path before timing.
Subsequent calls measure `PredictiveWatchOdds.cache_retrieved_at` with the same
ticker, input session, and hash. The filesystem fingerprint remains checked on
every hit.

| Contract lookups | Median (ms) | Min–max (ms) | `Path.stat` calls | Peak traced Python bytes |
| ---: | ---: | ---: | ---: | ---: |
| 250 | 5.165 | 5.020–7.891 | 500 | 4,791 |
| 1,000 | 21.342 | 20.011–26.110 | 2,000 | 4,679 |
| 5,000 | 144.372 | 131.253–159.415 | 10,000 | 4,615 |

Two filesystem checks per contract persist on cache hits. This is measurable
linear work at large counts, but it protects against a replaced price file or
manifest. Retain those checks. Any future request-scoped fingerprint reuse must
first define and test what happens when files change during that request.
These timings include counting-wrapper overhead and warm local filesystem
caches; cold storage, network volumes, and full price-cache verification are
outside this measurement.

## Offline Greeks replaced during live enrichment

The fixture has 1,000 unique synthetic calls, one expiry, coherent bids/asks,
positive open interest, a fixed date, a $50 underlying, and a 4% offline rate.
The benchmark calls the real assembly path and then replaces its seven offline
Greek fields, mirroring the overwrite in live request enrichment. It excludes
providers and the rest of live risk/forecast calculation.

| Assembly mode | Median (ms) | Min–max (ms) | Greek calculations | Peak traced Python bytes |
| --- | ---: | ---: | ---: | ---: |
| Cold, real Greeks | 78.282 | 72.126–117.648 | 1,000 | 7,127,519 |
| Warm `ContractMemo` | 6.843 | 5.786–7.561 | 0 | 4,080,583 |
| Cold, measurement-only empty-Greeks stub | 38.517 | 34.340–76.373 | 0 | 6,991,663 |

The stub is confined to this entrypoint; all 1,000 Greek entry calls still occur.
It is a lower-cost comparison, not a compatible replacement for direct offline
assembly. Cold Greek work is observable, but existing memo reuse already removes
it from warm calculation paths. Preserve offline behavior. Consider a live-only
assembly change only after realistic cold-request profiling demonstrates that
this CPU dominates useful latency and offline callers remain covered.

## Failed Treasury serialization

Three concurrent calls use the real `fetch_treasury_curve` with a fresh
process-local cache/lock and `httpx.MockTransport`. Each mocked provider attempt
waits 5 ms and raises `ReadTimeout`.

| Callers | Provider attempts | Maximum concurrent attempts | Median total (ms) | Min–max (ms) |
| ---: | ---: | ---: | ---: | ---: |
| 3 | 6 | 1 | 35.529 | 34.895–36.168 |

Both month attempts repeat for each caller after failure. The requests carry a
25-second HTTPX read timeout. The application separately limits the page pricing
wait to 2 seconds while shielding the background input task. Thus this synthetic
test confirms serialized repeated work but does not establish a page-duration
regression or the frequency of real Treasury outages. HTTPX timeouts are not a
single guaranteed wall-clock deadline. Keep provider policy unchanged; qualify a
negative-cache or sharing change using representative outage observations.

## Bundle and application load cost

The normal production Vite build took 3,364.086 ms in its single bounded sample.
Asset bytes below exclude source maps and font files; actual resource totals and
navigation timings are included in the entrypoint's JSON output.

| Asset | Raw bytes | Gzip bytes |
| --- | ---: | ---: |
| Main JavaScript | 580,383 | 180,471 |
| Lazy Watchlist JavaScript | 11,705 | 3,735 |
| Main CSS | 65,636 | 12,603 |

The static server uses `Cache-Control: no-store` and serves uncompressed assets.
Each run reloads the real app with mocked API responses and waits for the first
desktop row plus two animation frames. It uses a 1440×1000 viewport.

| Chain rows | Median load (ms) | Min–max (ms) | Mounted desktop rows | API requests across 9 loads |
| ---: | ---: | ---: | ---: | ---: |
| 250 | 554.498 | 529.798–589.234 | 250 | 18 |
| 1,000 | 535.463 | 514.663–551.305 | 250 | 18 |
| 5,000 | 633.250 | 583.266–700.073 | 250 | 18 |

All runs had zero external requests and zero browser page errors. Loopback,
warm operating-system caches, mocked JSON, production schema validation,
automation, and animation frames all affect these numbers. There is no vendor
latency, constrained-network model, cache-cold device, or attribution of JavaScript
parse time to a specific dependency. Keep existing lazy routing and progressive
rendering. Additional splitting requires a measured dependency or startup CPU
target rather than the bundle warning alone.

## Qualified evidence-path operation counts

The backend entrypoint also builds a real temporary ledger with one matured call,
11 current model issuances, one valid exact label, and one shared 4,096-scenario
distribution/close pair. This reproduces the original audit fixture and builds
all 36 reports at fixed `as_of`.

| Operation | Audited `ccc8912` | Implemented working tree |
| --- | ---: | ---: |
| Scalar query pages, including terminal pages | 72 | 72 |
| Scalar issuance rows read | 66 | 36 |
| Distribution reads | 11 | 1 |
| CRPS calls | 11 | 1 |
| CRPS calls while `DB_LOCK` held | 11 | 0 |
| Returned reports | 36 | 36 |

Before counts are from the recorded audit reproduction using this same fixture;
the entrypoint prints current counts and does not manufacture a pre-change
implementation. The current build median was 1,141.569 ms (971.789–1,257.418),
with 731,997 peak traced Python bytes. This tiny ledger is suitable for duplicate
work counts, not a representative daily-build latency estimate. It does not
exercise cache eviction, the 10,000-row reuse ceiling, native DuckDB memory, or
large historical ledgers; dedicated regression tests cover those correctness
boundaries. The separate 4,096-scenario ledger score measured 16.128 ms
(16.060–17.091), with 477,898 peak traced Python bytes.

Scalar issuance reads decrease by 45.5% in this fixture; query-page count is
unchanged. The intended improvement is fewer repeated rows/scorings and CPU work outside
database ownership. Timings do not quantify a production speedup. Python traced
memory excludes native DuckDB/Polars buffers and browser memory; count wrappers,
profiling, and memory tracing each have their own overhead.
