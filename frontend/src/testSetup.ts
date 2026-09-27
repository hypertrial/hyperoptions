import { afterEach } from "vitest"

if (typeof window !== "undefined") {
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
