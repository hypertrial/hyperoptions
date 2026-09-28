import { useSyncExternalStore } from "react"

export type Density = "compact" | "comfortable"

const STORAGE_KEY = "density"
const listeners = new Set<() => void>()
let sessionDensity: Density | null = null
let storageWriteFailed = false

function emit() {
  for (const listener of listeners) listener()
}

function readDensity(): Density {
  if (typeof window === "undefined") return "comfortable"
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY)
    if (storageWriteFailed && sessionDensity !== null) return sessionDensity
    sessionDensity = null
    return stored === "compact" ? "compact" : "comfortable"
  } catch { return sessionDensity ?? "comfortable" }
}

export function getDensity(): Density {
  return readDensity()
}

export function setDensity(density: Density) {
  if (typeof window === "undefined") return
  sessionDensity = density
  try {
    window.localStorage.setItem(STORAGE_KEY, density)
    storageWriteFailed = false
  } catch { storageWriteFailed = true }
  emit()
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  if (typeof window === "undefined") return () => listeners.delete(listener)
  const onStorage = (event: StorageEvent) => {
    if (event.key === STORAGE_KEY) {
      sessionDensity = null
      storageWriteFailed = false
      emit()
    }
  }
  window.addEventListener("storage", onStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener("storage", onStorage)
  }
}

export function useDensity() {
  const density = useSyncExternalStore(subscribe, getDensity, () => "comfortable" as const)
  return {
    density,
    toggle: () => setDensity(density === "compact" ? "comfortable" : "compact"),
  }
}
