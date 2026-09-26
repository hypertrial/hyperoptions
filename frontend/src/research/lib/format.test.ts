import { describe, expect, it } from "vitest"
import { formatInt, formatNum, formatPct, formatSignedPct } from "./format"

describe("formatters", () => {
  it("renders missing metrics as an em dash", () => {
    expect(formatPct(null)).toBe("—")
    expect(formatNum(undefined)).toBe("—")
    expect(formatInt(Number.NaN)).toBe("—")
  })

  it("formats ratios as percentages and leaves sharpe as a number", () => {
    expect(formatPct(0.1234)).toBe("12.3%")
    expect(formatPct(-0.6, 0)).toBe("-60%")
    expect(formatNum(1.256)).toBe("1.26")
    expect(formatInt(1200)).toBe("1,200")
  })

  it("prefixes a positive percent and leaves zero unsigned", () => {
    expect(formatSignedPct(0.247)).toBe("+24.7%")
    expect(formatSignedPct(-0.426)).toBe("-42.6%")
    expect(formatSignedPct(0)).toBe("0.0%")
    expect(formatSignedPct(null)).toBe("—")
    expect(formatSignedPct(Number.NaN)).toBe("—")
  })
})
