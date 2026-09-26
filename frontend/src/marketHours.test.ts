import { describe, expect, it } from "vitest"
import { isRegularMarketHours } from "./marketHours"

describe("regular market refresh window", () => {
  it("uses New York time across daylight saving changes", () => {
    expect(isRegularMarketHours(new Date("2026-09-17T13:29:00Z"))).toBe(false)
    expect(isRegularMarketHours(new Date("2026-09-17T13:30:00Z"))).toBe(true)
    expect(isRegularMarketHours(new Date("2026-09-17T20:00:00Z"))).toBe(false)
    expect(isRegularMarketHours(new Date("2026-01-12T14:30:00Z"))).toBe(true)
    expect(isRegularMarketHours(new Date("2026-01-17T15:00:00Z"))).toBe(false)
  })
})
