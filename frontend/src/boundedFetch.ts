export function boundedFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
  timeoutMs = 60_000,
): Promise<Response> {
  const deadline = AbortSignal.timeout(timeoutMs)
  const signal = init?.signal ? AbortSignal.any([init.signal, deadline]) : deadline
  return fetch(input, { ...init, signal })
}
