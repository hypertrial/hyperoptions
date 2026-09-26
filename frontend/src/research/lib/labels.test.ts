import { describe, expect, it } from "vitest"
import { FAMILIES, FLAGS, METRICS, SEGMENTS, defaultGates, exitLabel, familyLabel, flagLabel, segmentLabel } from "./labels"

describe("labels", () => {
  it("names every known family, flag, and segment", () => {
    for (const family of FAMILIES) expect(familyLabel(family).length).toBeGreaterThan(0)
    for (const flag of FLAGS) expect(flagLabel(flag).length).toBeGreaterThan(0)
    for (const segment of SEGMENTS) expect(segmentLabel(segment).length).toBeGreaterThan(0)
    expect(METRICS.holdoutCagr.label).toBe("Holdout CAGR")
  })

  it("embeds the gate thresholds in flag copy", () => {
    expect(flagLabel("insufficient_trades", defaultGates)).toContain(String(defaultGates.minTrades))
    expect(flagLabel("insufficient_trades", defaultGates)).toContain(String(defaultGates.minValTrades))
    expect(flagLabel("extreme_drawdown")).toContain("60%")
    expect(flagLabel("severe_degradation")).toContain("0.4")
    expect(flagLabel("unstable_parameters")).toContain("0.5")
    expect(flagLabel("no_neighbours")).toContain("neighbours")
  })

  it("calls the test segment the holdout", () => {
    expect(segmentLabel("test")).toBe("Holdout")
    expect(segmentLabel("validation")).toBe("Validation")
    expect(familyLabel("trend_following")).toBe("Trend following")
    expect(familyLabel("trend_momentum")).toBe("Trend + momentum")
    expect(exitLabel("atr_trail")).toBe("ATR trail")
    expect(exitLabel("time")).toBe("Time stop")
  })
})
