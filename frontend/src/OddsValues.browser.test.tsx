// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it } from "vitest"

import OddsValues from "./OddsValues"
import type { PredictiveOdds } from "./marketOdds"

afterEach(cleanup)

it("describes empirical support as overlapping samples and baseline support as daily returns", () => {
  const predictive: PredictiveOdds = {
    status: "available", method: "empirical_scaled", itm_pct_tenths: 500,
    otm_pct_tenths: 500, atm_pct_tenths: 0, as_of_session: "2026-09-18", support: 400,
  }
  const { rerender } = render(<OddsValues odds={{ status: "unavailable" }} predictiveOdds={predictive} compact />)
  expect(screen.getByText(/model support 400/).getAttribute("title")).toContain("overlapping")

  rerender(<OddsValues odds={{ status: "unavailable" }} predictiveOdds={{ ...predictive, method: "lognormal_ewma", support: 60 }} compact />)
  expect(screen.getByText(/model support 60/).getAttribute("title")).toContain("daily returns")
})
