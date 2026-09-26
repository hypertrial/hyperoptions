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
  return new ApiError(response.status, messageFromBody(await response.text()))
}

const REQUEST_MS = 60_000

async function request(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(path, { ...init, signal: AbortSignal.timeout(REQUEST_MS) })
  } catch (error) {
    if (error instanceof DOMException && error.name === "TimeoutError") {
      throw new ApiError(0, "The API did not respond.")
    }
    throw error
  }
}

export async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw await readError(response)
  return response.json() as Promise<T>
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw await readError(response)
  return response.json() as Promise<T>
}
