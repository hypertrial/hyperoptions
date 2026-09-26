import { getJson, postJson, readError } from "../research/api/client"
import type { JobView, WatchCreate } from "../generated/types.gen"
import type { AddWatchResponse, RefreshResponse, WatchlistResponse } from "./types"

export const WATCH_JOB_KEY = "hyperoptions.watchlist.job"

export const getWatchlist = () => getJson<WatchlistResponse>("/api/watchlist")
export const addWatch = (watchKey: string) => postJson<AddWatchResponse>("/api/watchlist", { watch_key: watchKey } satisfies WatchCreate)
export const refreshWatchlist = () => postJson<RefreshResponse>("/api/watchlist/refresh", {})
export const getJob = (id: string) => getJson<JobView>(`/api/jobs/${encodeURIComponent(id)}`)

export async function deleteWatch(id: string): Promise<void> {
  const response = await fetch(`/api/watchlist/${encodeURIComponent(id)}`, {
    method: "DELETE",
    signal: AbortSignal.timeout(60_000),
  })
  if (!response.ok) throw await readError(response)
}
