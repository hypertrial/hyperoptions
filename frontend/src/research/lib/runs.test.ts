import { describe, expect, it } from "vitest"
import { dataThrough, runLabel } from "./runs"

describe("runLabel", () => {
  it("includes the strategy count, ticker count, and latest marker", () => {
    const label = runLabel(
      {
        id: "abc",
        created_at: "2026-09-24T12:06:00Z",
        status: "completed",
        strategy_count: 2000,
        ticker_count: 4,
      },
      true,
    )
    expect(label).toContain("Sep")
    expect(label).toContain("2,000 strategies")
    expect(label).toContain("4 tickers")
    expect(label).toContain("latest")
    expect(runLabel(
      {
        id: "abc",
        created_at: "2026-09-24T12:06:00Z",
        status: "completed",
        strategy_count: 12,
        ticker_count: 1,
      },
      false,
    )).not.toContain("latest")
  })
})

describe("dataThrough", () => {
  it("uses the newest bar date", () => {
    expect(dataThrough(["2024-01-01", null, "2026-09-23"])).toBe("Data through 2026-09-23")
    expect(dataThrough([null, undefined])).toBe("No market data")
  })
})
