import type { ZodType } from "zod"
import { boundedFetch } from "../boundedFetch"

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

export function messageFromBody(body: string): string {
  if (!body.trim()) return "Request failed."
  try {
    const parsed = JSON.parse(body) as { detail?: unknown }
    if (typeof parsed.detail === "string" && parsed.detail) return parsed.detail
  } catch {
    return body
  }
  return body
}

export async function readError(response: Response): Promise<ApiError> {
  try {
    return new ApiError(response.status, messageFromBody(await response.text()))
  } catch (error) { rethrowTransport(error) }
}

function rethrowTransport(error: unknown): never {
  if (error instanceof Error && (error.name === "TimeoutError" || error.name === "AbortError")) {
    throw new ApiError(0, "The API did not respond.")
  }
  throw error
}

export async function request(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await boundedFetch(path, init)
  } catch (error) { rethrowTransport(error) }
}

async function readJson<T>(response: Response, schema: ZodType<T>): Promise<T> {
  let body: unknown
  try {
    body = await response.json()
  } catch (error) {
    if (error instanceof Error && (error.name === "TimeoutError" || error.name === "AbortError")) rethrowTransport(error)
    throw new Error("Local API returned an invalid response")
  }
  const parsed = schema.safeParse(body)
  if (!parsed.success) throw new Error("Local API returned an invalid response")
  return parsed.data
}

export async function getJson<T>(path: string, schema: ZodType<T>): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw await readError(response)
  return readJson(response, schema)
}

export async function postJson<T>(path: string, body: unknown, schema: ZodType<T>): Promise<T> {
  const response = await request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw await readError(response)
  return readJson(response, schema)
}
