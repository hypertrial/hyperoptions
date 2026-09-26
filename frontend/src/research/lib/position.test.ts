import { describe, expect, it } from "vitest"
import { currentPosition, safeReturnPath, tradeSegment } from "./position"

describe("currentPosition", () => {
  it("follows the last fill mark", () => {
    expect(currentPosition([1, null], [null, 2], ["2024-01-01", "2024-02-01"])).toEqual({
      side: "flat",
      date: "2024-02-01",
    })
    expect(currentPosition([null, 4], [1, null], ["2024-01-01", "2024-06-01"])).toEqual({
      side: "long",
      date: "2024-06-01",
    })
  })

  it("stays long when an entry has no later exit", () => {
    expect(currentPosition([null, 11], [null, null], ["2024-01-02", "2024-01-03"])).toEqual({
      side: "long",
      date: "2024-01-03",
    })
  })

  it("is flat with no date when nothing filled, and uses the overlapping prefix", () => {
    expect(currentPosition([], [], [])).toEqual({ side: "flat", date: null })
    expect(currentPosition([null, null], [null, null], ["2024-01-01", "2024-01-02"])).toEqual({
      side: "flat",
      date: null,
    })
    expect(currentPosition([1, 2], [null], ["2024-01-01", "2024-01-02"])).toEqual({
      side: "long",
      date: "2024-01-01",
    })
  })
})

describe("tradeSegment", () => {
  const bounds = {
    train: { start: "2020-01-01", end: "2023-01-01" },
    validation: { start: "2023-01-02", end: "2024-06-01" },
    test: { start: "2024-06-02", end: "2026-01-01" },
    full: { start: "2020-01-01", end: "2026-01-01" },
  }

  it("attributes the exit date to the segment that contains it", () => {
    expect(tradeSegment("2023-01-01", bounds)).toBe("train")
    expect(tradeSegment("2023-01-02", bounds)).toBe("validation")
    expect(tradeSegment("2024-06-02", bounds)).toBe("test")
  })

  it("leaves an exit after the holdout unlabeled", () => {
    expect(tradeSegment("2026-02-01", bounds)).toBe("")
  })
})

describe("safeReturnPath", () => {
  it("keeps an in-app leaderboard or overview path", () => {
    expect(safeReturnPath("/research/leaderboard?ticker=IREN&run=abc", "CIFR", "abc")).toBe(
      "/research/leaderboard?ticker=IREN&run=abc",
    )
    expect(safeReturnPath("/research", "IREN", "abc")).toBe("/research")
  })

  it("ignores an outside path", () => {
    expect(safeReturnPath("https://example.com", "IREN", "abc")).toBe("/research/leaderboard?ticker=IREN&run=abc")
    expect(safeReturnPath(undefined, "", "")).toBe("/research/leaderboard")
  })
})
