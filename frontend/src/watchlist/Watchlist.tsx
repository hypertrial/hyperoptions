import { useCallback, useEffect, useRef, useState } from "react"
import { Link } from "react-router-dom"
import { Button } from "@/components/ui/button"
import type { Job } from "../generated/types.gen"
import { dateTime } from "../format"
import { deleteWatch, getJob, getWatchlist, refreshWatchlist, WATCH_JOB_KEY } from "./api"
import type { WatchItem, WatchlistResponse } from "./types"
import WatchCard from "./WatchCard"

function message(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "The request failed."
}

export default function Watchlist({ chainUrl = "/" }: { chainUrl?: string }) {
  const [items, setItems] = useState<WatchItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [staleError, setStaleError] = useState<string | null>(null)
  const [lastLoadedAt, setLastLoadedAt] = useState<string | null>(null)
  const [retrying, setRetrying] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [deleting, setDeleting] = useState<string | null>(null)
  const [deleteErrors, setDeleteErrors] = useState<Record<string, string>>({})
  const [jobId, setJobId] = useState<string | null>(() => sessionStorage.getItem(WATCH_JOB_KEY))
  const [job, setJob] = useState<Job | null>(null)
  const mutationVersion = useRef(0)
  const pendingLoad = useRef<Promise<void> | null>(null)

  const applyResponse = useCallback((response: WatchlistResponse) => {
    setItems(response.items)
    setError(null)
    setStaleError(null)
    setLastLoadedAt(new Date().toISOString())
    const active = response.active_job
    if (active && (active.state === "queued" || active.state === "running")) {
      sessionStorage.setItem(WATCH_JOB_KEY, active.id)
      setJob(active)
      setJobId(active.id)
    }
  }, [])

  const load = useCallback(() => {
    if (pendingLoad.current) return pendingLoad.current
    const version = mutationVersion.current
    const pending = getWatchlist()
      .then((response) => { if (version === mutationVersion.current) applyResponse(response) })
      .finally(() => { pendingLoad.current = null })
    pendingLoad.current = pending
    return pending
  }, [applyResponse])

  useEffect(() => {
    let active = true
    let busy = false
    let first = true
    let timer: number | undefined
    const poll = async () => {
      if (!active || busy || document.hidden) return
      busy = true
      try {
        await load()
      } catch (cause) {
        if (active && first) setError(message(cause))
        else if (active) setStaleError(message(cause))
      } finally {
        if (active && first) setLoading(false)
        first = false
        busy = false
        if (active && !document.hidden) timer = window.setTimeout(() => { void poll() }, 15_000)
      }
    }
    const onVisibility = () => {
      if (timer != null) window.clearTimeout(timer)
      if (!document.hidden) void poll()
    }
    if (!document.hidden) void poll()
    document.addEventListener("visibilitychange", onVisibility)
    return () => {
      active = false
      if (timer != null) window.clearTimeout(timer)
      document.removeEventListener("visibilitychange", onVisibility)
    }
  }, [load])

  useEffect(() => {
    if (!jobId) return
    let active = true
    let timer: number | undefined
    const poll = async () => {
      try {
        const current = await getJob(jobId)
        if (!active) return
        setJob(current)
        if (current.state === "queued" || current.state === "running") {
          timer = window.setTimeout(() => { void poll() }, 1500)
          return
        }
        sessionStorage.removeItem(WATCH_JOB_KEY)
        setJobId(null)
        setNotice(current.state === "failed" ? current.error || "Expiry result check failed." : "Expiry results checked.")
        await load()
      } catch (cause) {
        if (!active) return
        sessionStorage.removeItem(WATCH_JOB_KEY)
        setJobId(null)
        setNotice(message(cause))
      }
    }
    void poll()
    return () => { active = false; if (timer != null) window.clearTimeout(timer) }
  }, [jobId, load])

  const refresh = async () => {
    setRefreshing(true)
    setNotice(null)
    try {
      const result = await refreshWatchlist()
      if (result.job) {
        sessionStorage.setItem(WATCH_JOB_KEY, result.job.id)
        setJob(result.job)
        setJobId(result.job.id)
      } else {
        setNotice("No expiry results are due.")
        await load()
      }
    } catch (cause) {
      setNotice(message(cause))
    } finally {
      setRefreshing(false)
    }
  }

  const retryStale = async () => {
    setRetrying(true)
    try {
      await load()
    } catch (cause) {
      setStaleError(message(cause))
    } finally {
      setRetrying(false)
    }
  }

  const remove = async (id: string) => {
    setDeleting(id)
    setDeleteErrors((current) => { const next = { ...current }; delete next[id]; return next })
    try {
      await deleteWatch(id)
      mutationVersion.current += 1
      setItems((current) => current.filter((item) => item.id !== id))
    } catch (cause) {
      setDeleteErrors((current) => ({ ...current, [id]: message(cause) }))
    } finally {
      setDeleting(null)
    }
  }

  const jobBusy = jobId != null && (!job || job.state === "queued" || job.state === "running")
  return (
    <main id="main-content" className="watchlist-page">
      <header className="watchlist-header">
        <div>
          <p className="eyebrow">Selected option contracts</p>
          <h1>Watchlist</h1>
          <p>Watches are for tracking contracts, not trades or positions. No holdings or premiums are tracked.</p>
          <p>Choose contracts from the <Link to={chainUrl}>option chain</Link>. Current option quotes provide market-implied odds when reliable; otherwise a separately labeled historical stock-close forecast may be shown. Estimates and hypothetical risk use dated public data.</p>
        </div>
        <Button type="button" variant="outline" disabled={refreshing || jobBusy} onClick={() => { void refresh() }}>
          {refreshing ? "Starting…" : jobBusy ? "Check running" : "Check expiry results"}
        </Button>
      </header>

      {jobBusy && job ? (
        <section className="watch-job" aria-label="Watchlist job progress">
          <p>Expiry result check: {job.message ?? "queued"} ({Math.round((job.progress ?? 0) * 100)}%)</p>
          <div role="progressbar" aria-label="Watchlist refresh progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round((job.progress ?? 0) * 100)}>
            <span style={{ width: `${Math.round((job.progress ?? 0) * 100)}%` }} />
          </div>
        </section>
      ) : null}
      {jobBusy && !job ? <p role="status">Checking expiry results…</p> : null}
      {notice ? <p className="watch-notice" role="status">{notice}</p> : null}
      {loading ? <p role="status">Loading watched contracts…</p> : null}
      {error ? <div className="watch-error" role="alert"><p>Could not load the watchlist. {error}</p><Button variant="outline" type="button" onClick={() => { setLoading(true); void load().catch((cause) => setError(message(cause))).finally(() => setLoading(false)) }}>Retry</Button></div> : null}
      {staleError && !error ? <div className="watch-error" role="alert"><p>Could not refresh the watchlist. Showing the last loaded watchlist{lastLoadedAt ? ` from ${dateTime(lastLoadedAt)}` : ""}. {staleError}</p><Button variant="outline" type="button" disabled={retrying} onClick={() => { void retryStale() }}>{retrying ? "Retrying…" : "Retry"}</Button></div> : null}
      {!loading && !error && items.length === 0 ? (
        <div className="watch-empty"><h2>No watched contracts yet</h2><p>Open the option chain and use Watch on a supported contract.</p><Link to={chainUrl}>Browse option chain</Link></div>
      ) : null}
      {!loading && !error && items.length > 0 ? (
        <div className="watch-card-list">
          {items.map((item) => <WatchCard key={item.id} item={item} deleting={deleting === item.id} deleteError={deleteErrors[item.id] ?? null} onDelete={(id) => { void remove(id) }} />)}
        </div>
      ) : null}
    </main>
  )
}
