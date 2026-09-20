import { useSyncExternalStore } from "react"

export type ThemePreference = "light" | "dark" | "system"

const STORAGE_KEY = "theme"
const listeners = new Set<() => void>()

function emit() {
  for (const listener of listeners) listener()
}

function readPreference(): ThemePreference {
  if (typeof window === "undefined") return "system"
  const stored = window.localStorage.getItem(STORAGE_KEY)
  if (stored === "light" || stored === "dark" || stored === "system") return stored
  return "system"
}

function systemPrefersDark(): boolean {
  if (typeof window === "undefined") return false
  return window.matchMedia("(prefers-color-scheme: dark)").matches
}

export function resolvedDark(preference: ThemePreference): boolean {
  return preference === "dark" || (preference === "system" && systemPrefersDark())
}

function applyClass(preference: ThemePreference) {
  if (typeof document === "undefined") return
  document.documentElement.classList.toggle("dark", resolvedDark(preference))
}

export function getThemePreference(): ThemePreference {
  return readPreference()
}

export function setThemePreference(preference: ThemePreference) {
  if (typeof window === "undefined") return
  window.localStorage.setItem(STORAGE_KEY, preference)
  applyClass(preference)
  emit()
}

export function cycleTheme(preference: ThemePreference): ThemePreference {
  if (preference === "system") return "dark"
  if (preference === "dark") return "light"
  return "system"
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  if (typeof window === "undefined") return () => listeners.delete(listener)
  const media = window.matchMedia("(prefers-color-scheme: dark)")
  const onMedia = () => {
    applyClass(readPreference())
    emit()
  }
  const onStorage = (event: StorageEvent) => {
    if (event.key === STORAGE_KEY) {
      applyClass(readPreference())
      emit()
    }
  }
  media.addEventListener("change", onMedia)
  window.addEventListener("storage", onStorage)
  return () => {
    listeners.delete(listener)
    media.removeEventListener("change", onMedia)
    window.removeEventListener("storage", onStorage)
  }
}

export function useTheme() {
  const preference = useSyncExternalStore(subscribe, getThemePreference, () => "system" as const)
  return {
    preference,
    dark: resolvedDark(preference),
    setPreference: setThemePreference,
    cycle: () => setThemePreference(cycleTheme(preference)),
  }
}
