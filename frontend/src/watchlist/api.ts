import { getJson, postJson, readError, request } from "./client"
import { DEFAULT_FORECAST_MODEL, type PhysicalModel } from "../forecastModels"
import type { Job, WatchCreate } from "../generated/types.gen"
import type { AddWatchResponse, RefreshResponse, WatchlistResponse } from "./types"
import { zJob, zWatchCreateResponse, zWatchListResponse, zWatchRefreshResponse } from "../generated/zod.gen"

const modelQuery = (model: PhysicalModel) => model === DEFAULT_FORECAST_MODEL ? "" : `?forecast_model=${model}`
export const getWatchlist = (model: PhysicalModel = DEFAULT_FORECAST_MODEL): Promise<WatchlistResponse> => getJson(`/api/watchlist${modelQuery(model)}`, zWatchListResponse)
export const addWatch = (watchKey: string, model: PhysicalModel = DEFAULT_FORECAST_MODEL): Promise<AddWatchResponse> => postJson(`/api/watchlist${modelQuery(model)}`, { watch_key: watchKey } satisfies WatchCreate, zWatchCreateResponse)
export const refreshWatchlist = (): Promise<RefreshResponse> => postJson("/api/watchlist/refresh", {}, zWatchRefreshResponse)
export const getJob = (id: string): Promise<Job> => getJson(`/api/jobs/${encodeURIComponent(id)}`, zJob)

export async function deleteWatch(id: string): Promise<void> {
  const response = await request(`/api/watchlist/${encodeURIComponent(id)}`, {
    method: "DELETE",
  })
  if (!response.ok) throw await readError(response)
}
