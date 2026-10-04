import { afterEach } from "vitest"

function memoryStorage(): Storage {
  const store = new Map<string, string>()
  return {
    get length() { return store.size },
    clear: () => store.clear(),
    getItem: (key: string) => store.get(key) ?? null,
    key: (index: number) => [...store.keys()][index] ?? null,
    removeItem: (key: string) => { store.delete(key) },
    setItem: (key: string, value: string) => { store.set(key, String(value)) },
  }
}

function storageWorks(storage: Storage | null | undefined): storage is Storage {
  if (storage == null || typeof storage.getItem !== "function" || typeof storage.setItem !== "function") return false
  try {
    storage.setItem("__hyperoptions_storage_probe__", "1")
    const ok = storage.getItem("__hyperoptions_storage_probe__") === "1"
    storage.removeItem("__hyperoptions_storage_probe__")
    return ok
  } catch {
    return false
  }
}

if (typeof window !== "undefined") {
  if (!storageWorks(window.localStorage)) {
    Object.defineProperty(window, "localStorage", { configurable: true, value: memoryStorage() })
  }
  if (!storageWorks(globalThis.localStorage)) {
    Object.defineProperty(globalThis, "localStorage", { configurable: true, value: window.localStorage })
  }
  if (typeof window.matchMedia !== "function") {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      configurable: true,
      value: (query: string) => ({
        matches: query.includes("min-width: 64rem"),
        media: query,
        onchange: null,
        addEventListener() {},
        removeEventListener() {},
        addListener() {},
        removeListener() {},
        dispatchEvent() { return false },
      }),
    })
  }
  if (typeof window.ResizeObserver !== "function") {
    Object.defineProperty(window, "ResizeObserver", {
      writable: true,
      configurable: true,
      value: class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    })
  }
  if (typeof window.HTMLDialogElement !== "undefined" && !HTMLDialogElement.prototype.showModal) {
    HTMLDialogElement.prototype.showModal = function () { this.open = true }
    HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new Event("close")) }
  }
}

afterEach(() => {
  if (typeof window === "undefined") return
  window.localStorage.clear()
  document.documentElement.classList.remove("dark")
})
