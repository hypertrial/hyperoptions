import { getJson, postJson, readError } from "./client"
import { DEFAULT_FORECAST_MODEL, type PhysicalModel } from "../forecastModels"
import type { Job, WatchCreate } from "../generated/types.gen"
import type { AddWatchResponse, RefreshResponse, WatchlistResponse } from "./types"

export const WATCH_JOB_KEY = "hyperoptions.watchlist.job"

const modelQuery = (model: PhysicalModel) => model === DEFAULT_FORECAST_MODEL ? "" : `?forecast_model=${model}`
export const getWatchlist = (model: PhysicalModel = DEFAULT_FORECAST_MODEL) => getJson<WatchlistResponse>(`/api/watchlist${modelQuery(model)}`)
export const addWatch = (watchKey: string, model: PhysicalModel = DEFAULT_FORECAST_MODEL) => postJson<AddWatchResponse>(`/api/watchlist${modelQuery(model)}`, { watch_key: watchKey } satisfies WatchCreate)
export const refreshWatchlist = () => postJson<RefreshResponse>("/api/watchlist/refresh", {})
export const getJob = (id: string) => getJson<Job>(`/api/jobs/${encodeURIComponent(id)}`)

export async function deleteWatch(id: string): Promise<void> {
  const response = await fetch(`/api/watchlist/${encodeURIComponent(id)}`, {
    method: "DELETE",
    signal: AbortSignal.timeout(60_000),
  })
  if (!response.ok) throw await readError(response)
}
