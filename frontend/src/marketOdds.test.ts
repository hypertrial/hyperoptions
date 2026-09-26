import { expect, it } from "vitest"

import { preferredOddsKind, predictiveAvailable } from "./marketOdds"
import type { MarketOddsView, PredictiveOddsView } from "./generated/types.gen"

it("keeps market-implied odds primary and accepts predictive odds only as a complete partition", () => {
  const market: MarketOddsView = { status: "available", itm_pct_tenths: 638, otm_pct_tenths: 362 }
  const predictive: PredictiveOddsView = { status: "available", itm_pct_tenths: 520, otm_pct_tenths: 470, atm_pct_tenths: 10 }
  expect(preferredOddsKind(market, predictive)).toBe("market")
  expect(preferredOddsKind({ status: "unavailable" }, predictive)).toBe("predictive")
  expect(predictiveAvailable({ ...predictive, atm_pct_tenths: 11 })).toBe(false)
  expect(preferredOddsKind({ status: "unavailable" }, { ...predictive, atm_pct_tenths: 11 })).toBeNull()
})
