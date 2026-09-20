import { useSyncExternalStore } from "react"

function subscribe(query: string, listener: () => void) {
  const media = window.matchMedia(query)
  media.addEventListener("change", listener)
  return () => media.removeEventListener("change", listener)
}

export function useMediaQuery(query: string, serverFallback = false) {
  return useSyncExternalStore(
    (listener) => subscribe(query, listener),
    () => window.matchMedia(query).matches,
    () => serverFallback,
  )
}
