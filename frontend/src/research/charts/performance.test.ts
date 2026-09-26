import { expect, it } from "vitest"

import type { StrategyDetail } from "../api/types"
import { performanceFigure } from "./performance"

it("uses a visible close series in both themes", () => {
  const detail = {
    segment_bounds: {}, dates: [], close: [], entry_marks: [], exit_marks: [],
    equity: [], buy_hold: [], drawdown: [],
  } as unknown as StrategyDetail
  const closeColor = (dark: boolean) =>
    (performanceFigure(detail, dark).data[0] as { line: { color: string } }).line.color

  expect(closeColor(false)).toBe("#1c1612")
  expect(closeColor(true)).toBe("#f1f5f9")
})
