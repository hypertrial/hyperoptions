import { useQuery } from "@tanstack/react-query"
import { useRef } from "react"
import { Link, NavLink, Outlet, useSearchParams } from "react-router-dom"
import { getJson } from "../api/client"
import type { Run, TickerStatus } from "../api/types"
import { useJob } from "../jobs/JobProvider"
import { dataThrough, runLabel } from "../lib/runs"
import { Alert, Notice } from "./ui/notice"
import { Button } from "./ui/button"

export function Shell() {
  const [params, setParams] = useSearchParams()
  const runs = useQuery({ queryKey: ["runs"], queryFn: () => getJson<Run[]>("/api/research/runs") })
  const status = useQuery({ queryKey: ["status"], queryFn: () => getJson<TickerStatus[]>("/api/research/data/status") })
  const { job, busy, notice, start, dismiss } = useJob()
  const runId = params.get("run") || runs.data?.[0]?.id || ""

  function selectRun(value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set("run", value)
    else next.delete("run")
    next.delete("page")
    setParams(next)
  }

  const navClass = ({ isActive }: { isActive: boolean }) =>
    `inline-flex min-h-6 items-center rounded-md px-2 py-1 ${isActive ? "font-medium text-foreground underline decoration-2 underline-offset-4" : "text-foreground hover:text-foreground"}`

  return (
    <div className="min-h-screen">
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-7xl flex-col gap-3 px-4 py-4 sm:px-6">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <Link to={runId ? `/research?run=${runId}` : "/research"} className="font-sans text-2xl">
              StockSweeper Research
            </Link>
            <nav className="flex gap-1 text-sm">
              <NavLink end to={runId ? `/research?run=${runId}` : "/research"} className={navClass}>
                Overview
              </NavLink>
              <NavLink to={runId ? `/research/leaderboard?run=${runId}` : "/research/leaderboard"} className={navClass}>
                Leaderboard
              </NavLink>
            </nav>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
            <span className="w-fit rounded-full border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground">
              {status.isError ? "API unavailable" : dataThrough((status.data ?? []).map((item) => item.last))}
            </span>
            <RunMenu
              runs={runs.data ?? []}
              loading={runs.isLoading}
              failed={runs.isError}
              runId={runId}
              onSelect={selectRun}
            />
            <div className="flex shrink-0 gap-2">
              <Button variant="outline" disabled={busy} onClick={() => start("/api/research/data/update", { full_refresh: false })}>
                Update Data
              </Button>
              <Button disabled={busy} onClick={() => start("/api/research/backtest/run", {})}>
                Run Backtest
              </Button>
            </div>
          </div>
        </div>
        {busy && job ? (
          <div className="border-t border-border">
            <div
              className="h-1 bg-border"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(job.progress * 100)}
              aria-label={job.message}
            >
              <div className="h-1 bg-primary" style={{ width: `${Math.round(job.progress * 100)}%` }} />
            </div>
            <p className="px-6 py-2 text-sm">
              {job.message} ({Math.round(job.progress * 100)}%)
            </p>
          </div>
        ) : null}
        {notice ? (
          <Notice tone={notice.tone} onDismiss={dismiss}>
            {notice.text}
          </Notice>
        ) : null}
        {runs.isError || status.isError ? (
          <div className="border-t border-border px-4 py-2 sm:px-6">
            <Alert>{failureText(runs.error ?? status.error)}</Alert>
          </div>
        ) : null}
      </header>
      <main id="main-content" className="mx-auto max-w-7xl px-4 py-8 sm:px-6">
        <Outlet context={{ runId }} />
      </main>
    </div>
  )
}

function failureText(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "The API did not respond."
}

function RunMenu({
  runs,
  loading,
  failed,
  runId,
  onSelect,
}: {
  runs: Run[]
  loading: boolean
  failed: boolean
  runId: string
  onSelect: (value: string) => void
}) {
  const menu = useRef<HTMLDetailsElement>(null)
  const index = runs.findIndex((run) => run.id === runId)
  const current = index >= 0 ? runs[index] : undefined
  const label = loading ? "Loading runs…" : failed ? "API unavailable" : current ? runLabel(current, index === 0) : "No runs yet"

  return (
    <details ref={menu} className="relative min-w-0 sm:max-w-xl">
      <summary className="flex min-h-9 cursor-pointer list-none items-center justify-between gap-2 rounded-md border border-border bg-muted px-2 py-1 text-sm text-foreground [&::-webkit-details-marker]:hidden">
        <span className="min-w-0 flex-1">
          <span className="mr-2 text-xs text-muted-foreground">Run</span>
          {label}
        </span>
        <span aria-hidden="true" className="text-muted-foreground">
          ▾
        </span>
      </summary>
      {runs.length ? (
        <div className="absolute z-20 mt-1 max-h-64 w-full overflow-auto rounded-md border border-border bg-card shadow-sm sm:w-max sm:min-w-full">
          {runs.map((run, itemIndex) => (
            <button
              key={run.id}
              type="button"
              className="block min-h-9 w-full px-3 py-2 text-left text-sm hover:bg-muted"
              onClick={() => {
                onSelect(run.id)
                if (menu.current) menu.current.open = false
              }}
            >
              {runLabel(run, itemIndex === 0)}
            </button>
          ))}
        </div>
      ) : null}
    </details>
  )
}
