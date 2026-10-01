// Opt-in production measurements: node scripts/benchmark-audit-paths.mjs (Node 22).
// Existing synthetic test fixtures only; all browser API traffic is mocked.
import { execFileSync } from "node:child_process"
import { createServer } from "node:http"
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { gzipSync } from "node:zlib"
import { chromium } from "@playwright/test"
import { build } from "vite"

if (Number(process.versions.node.split(".")[0]) !== 22) throw new Error("Use Node 22")
const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const temporary = await mkdtemp(path.join(tmpdir(), "hyperoptions-audit-benchmark-"))
const appOutput = path.join(temporary, "app")
const harnessOutput = path.join(temporary, "harness")
const warmups = 2
const samples = 7
const capMs = 60_000
const summary = (values) => {
  const sorted = [...values].sort((a, b) => a - b)
  return { samples_ms: values, median_ms: sorted[Math.floor(sorted.length / 2)], min_ms: sorted[0], max_ms: sorted.at(-1) }
}
const capped = async (work) => {
  let timer
  try {
    return await Promise.race([
      work(),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("Phase exceeded 60 seconds")), capMs) }),
    ])
  } finally { clearTimeout(timer) }
}

let server
let browser
try {
  const buildStart = performance.now()
  // Build the normal app with its normal configuration into temporary output.
  execFileSync(process.execPath, [path.join(frontend, "node_modules/vite/bin/vite.js"), "build",
    "--outDir", appOutput, "--emptyOutDir", "--logLevel", "silent"], {
    cwd: frontend, timeout: capMs, stdio: "pipe",
  })
  const appBuildMs = performance.now() - buildStart
  const harnessEntry = path.join(temporary, "benchmark.html")
  await writeFile(harnessEntry, `<html><head><title>Offline benchmark</title></head><body><script type="module" src="./benchmark.ts"></script></body></html>`)
  await writeFile(path.join(temporary, "benchmark.ts"), `
import { largeChainPage } from ${JSON.stringify(path.join(frontend, "src/testFixtures.ts"))};
import { deriveChainView, INITIAL_REVEAL } from ${JSON.stringify(path.join(frontend, "src/viewModel.ts"))};
import { visibleColumns } from ${JSON.stringify(path.join(frontend, "src/columns.ts"))};
const filters = { premium: null, apr: null, breakeven: null, minIv: null, maxDte: null };
window.auditBenchmark = {
  fixture(size) {
    const page = largeChainPage(size);
    for (const group of page.expirations) for (const row of group.contracts) {
      row.market_odds = { status: "unavailable", reason: "offline benchmark" };
      row.predictive_odds = { status: "unavailable", reason: "offline benchmark" };
    }
    return page;
  },
  derive(size) {
    const page = largeChainPage(size);
    const columns = visibleColumns("call");
    const timings = [];
    let view;
    const deadline = performance.now() + 60_000;
    for (let sample = 0; sample < 9; sample++) {
      if (performance.now() >= deadline) throw new Error("Derivation phase exceeded 60 seconds");
      const start = performance.now();
      for (let repeat = 0; repeat < 20; repeat++) {
        if (performance.now() >= deadline) throw new Error("Derivation phase exceeded 60 seconds");
        view = deriveChainView(page, 1, filters, INITIAL_REVEAL, "call", columns);
      }
      if (sample >= 2) timings.push((performance.now() - start) / 20);
    }
    let accessorCalls = 0;
    const counted = columns.map(column => ({ ...column, accessor: row => { accessorCalls++; return column.accessor(row); } }));
    deriveChainView(page, 1, filters, INITIAL_REVEAL, "call", counted);
    return { samples: timings, rows_scanned_per_call: view.providerCount, visible_rows: view.visibleCount,
      mounted_rows: view.mountedCount, column_accessor_calls_separate_sample: accessorCalls, calls_per_sample: 20 };
  }
};
`)
  await capped(() => build({
    configFile: false, root: temporary, base: "/harness/", logLevel: "silent",
    build: { outDir: harnessOutput, emptyOutDir: true, rolldownOptions: { input: harnessEntry } },
  }))
  const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2" }
  server = createServer(async (request, response) => {
    const url = new URL(request.url, "http://127.0.0.1")
    const isHarness = url.pathname.startsWith("/harness/")
    const root = isHarness ? harnessOutput : appOutput
    const requested = isHarness ? url.pathname.slice("/harness/".length) : url.pathname.slice(1)
    const resolved = path.resolve(root, requested || "index.html")
    if (!resolved.startsWith(`${root}${path.sep}`)) { response.writeHead(404).end(); return }
    try {
      const file = await readFile(resolved)
      response.writeHead(200, { "Content-Type": mime[path.extname(resolved)] || "application/octet-stream", "Cache-Control": "no-store" })
      response.end(file)
    } catch {
      if (!isHarness && !path.extname(requested)) {
        response.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-store" })
        response.end(await readFile(path.join(appOutput, "index.html")))
      } else response.writeHead(404).end()
    }
  })
  await new Promise((resolve, reject) => {
    server.once("error", reject)
    server.listen(0, "127.0.0.1", resolve)
  })
  const origin = `http://127.0.0.1:${server.address().port}`
  browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 },
    permissions: ["clipboard-read", "clipboard-write"] })
  let currentFixture
  let apiRequests = 0
  const externalRequests = []
  await context.route("**/*", async route => {
    const url = new URL(route.request().url())
    if (url.origin !== origin) { externalRequests.push(url.origin); await route.abort(); return }
    if (!url.pathname.startsWith("/api/")) { await route.continue(); return }
    apiRequests++
    let body
    if (url.pathname.startsWith("/api/covered-calls/")) body = currentFixture
    else if (url.pathname === "/api/tickers") body = { as_of: "2026-10-01T14:00:00Z", total: 1, results: [{ symbol: "IREN", name: "Offline synthetic fixture" }] }
    else if (url.pathname === "/api/version") body = { status: "current", frontend_matches: true, running_sha: null, remote_sha: null }
    else if (url.pathname === "/api/watchlist") body = { items: [] }
    else throw new Error(`Unexpected benchmark API request: ${url.pathname}`)
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) })
  })
  const harness = await context.newPage()
  await harness.goto(`${origin}/harness/benchmark.html`)
  await harness.waitForFunction(() => Boolean(window.auditBenchmark))
  const page = await context.newPage()
  page.setDefaultTimeout(10_000)
  const errors = []
  page.on("pageerror", error => errors.push(error.message))
  const measurements = []
  for (const rows of [250, 1000, 5000]) {
    const derived = await capped(() => harness.evaluate(size => window.auditBenchmark.derive(size), rows))
    measurements.push({ phase: `complete_chain_derivation_${rows}`, ...summary(derived.samples),
      operations: { ...derived, samples: undefined } })
    currentFixture = await harness.evaluate(size => window.auditBenchmark.fixture(size), rows)
    const loads = []
    let navigation
    let resourceBytes
    let renderedRows
    const requestsBefore = apiRequests
    await capped(async () => {
      for (let sample = 0; sample < warmups + samples; sample++) {
        const start = performance.now()
        await page.goto(`${origin}/?t=IREN`, { waitUntil: "load" })
        await page.locator(".table-scroll tbody tr").first().waitFor()
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
        const elapsed = performance.now() - start
        if (sample >= warmups) loads.push(elapsed)
      }
      renderedRows = await page.locator(".table-scroll tbody tr").count()
      ;({ navigation, resourceBytes } = await page.evaluate(() => ({
        navigation: performance.getEntriesByType("navigation")[0]?.toJSON(),
        resourceBytes: performance.getEntriesByType("resource").reduce((sum, entry) => sum + entry.transferSize, 0),
      })))
    })
    measurements.push({ phase: `mocked_app_load_${rows}`, ...summary(loads),
      operations: { input_rows: rows, mounted_desktop_rows: renderedRows, api_requests_nine_loads: apiRequests - requestsBefore },
      last_navigation: navigation, last_resource_transfer_bytes: resourceBytes })
    for (const action of ["density", "copy"]) {
      const timings = []
      await capped(async () => {
        for (let sample = 0; sample < warmups + samples; sample++) {
          const start = performance.now()
          await (action === "density" ? page.getByRole("button", { name: /^Density:/ })
            : page.getByRole("button", { name: /^Copy row/ }).first()).click()
          await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
          if (sample >= warmups) timings.push(performance.now() - start)
        }
      })
      measurements.push({ phase: `app_${action}_${rows}`, ...summary(timings),
        operations: { input_rows: rows, actions_per_sample: 1, mounted_desktop_rows: renderedRows } })
    }
  }
  const assets = []
  for (const name of (await readdir(path.join(appOutput, "assets"))).sort()) {
    if (!/\.(js|css)$/.test(name)) continue
    const buffer = await readFile(path.join(appOutput, "assets", name))
    assets.push({ name, raw_bytes: buffer.length, gzip_bytes: gzipSync(buffer).length })
  }
  if (errors.length || externalRequests.length) throw new Error(JSON.stringify({ errors, externalRequests }))
  console.log(JSON.stringify({
    runtime: process.version, platform: `${process.platform} ${process.arch}`, browser: browser.version(),
    mode: "local production Vite builds; no-store static HTTP; headless Chromium; mocked APIs",
    warmups, samples, phase_cap_seconds: 60, app_build_ms_single_sample: appBuildMs,
    assets, measurements, external_requests: externalRequests, browser_errors: errors,
    limitations: ["Loopback network, warm OS cache, no vendor or slow-network latency.",
      "Interaction timings include Playwright and two animation frames, not exclusive derivation CPU.",
      "Pure derivation uses the same existing synthetic fixture, in a separate production harness.",
      "The existing 5,000-row fixture includes nonpositive terminal strikes; it is a CPU-size fixture, not market data.",
      "Build time is one bounded sample; Python/native/browser memory are not directly comparable."],
  }, null, 2))
} finally {
  await browser?.close()
  if (server) await new Promise(resolve => server.close(resolve))
  await rm(temporary, { recursive: true, force: true })
}
