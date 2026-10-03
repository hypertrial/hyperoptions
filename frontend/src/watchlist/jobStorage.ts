const WATCH_JOB_KEY = "hyperoptions.watchlist.job"

export function readWatchJob(): string | null {
  try {
    return sessionStorage.getItem(WATCH_JOB_KEY)
  } catch {
    return null
  }
}

export function saveWatchJob(id: string | null): void {
  try {
    if (id === null) sessionStorage.removeItem(WATCH_JOB_KEY)
    else sessionStorage.setItem(WATCH_JOB_KEY, id)
  } catch {
    // Job persistence is optional; API results and in-memory state still apply.
  }
}
