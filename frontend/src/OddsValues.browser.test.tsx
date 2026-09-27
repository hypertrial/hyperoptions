// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it } from "vitest"

import OddsValues from "./OddsValues"
import type { PredictiveOdds } from "./marketOdds"

afterEach(cleanup)

it("shows only ITM and OTM in the compact stock forecast", () => {
  const predictive: PredictiveOdds = {
    status: "available", method: "empirical_scaled", itm_pct_tenths: 500,
    otm_pct_tenths: 490, atm_pct_tenths: 10, as_of_session: "2026-09-18", support: 400,
  }
  const { container } = render(<OddsValues odds={{ status: "available", itm_pct_tenths: 700, otm_pct_tenths: 300, bound_low_pct_tenths: 650, bound_high_pct_tenths: 750 }} predictiveOdds={predictive} compact />)
  expect(container.querySelector(".odds-physical")?.textContent).toContain("50.0% ITM")
  expect(container.querySelector(".odds-physical")?.textContent).toContain("49.0% OTM")
  expect(container.querySelector(".odds-physical")?.textContent).not.toContain("ATM")
  expect(container.querySelector(".odds-physical")?.textContent).toContain("Stock close · Sep 18, 2026")
  expect(container.querySelector(".odds-market")?.textContent).toContain("70.0% ITM")
  expect(container.querySelector(".odds-values")?.firstElementChild?.className).toBe("odds-physical")
  expect(screen.queryByText("Reliability not yet established")).toBeNull()
  expect(container.textContent).not.toContain("support 400")
})

it("keeps the dated watchlist forecast and its reliability caveat without showing ATM", () => {
  render(<OddsValues odds={{ status: "unavailable" }} predictiveOdds={{
    status: "available", itm_pct_tenths: 500, otm_pct_tenths: 490,
    atm_pct_tenths: 10, as_of_session: "2026-09-18",
  }} />)
  expect(screen.getByText("Stock forecast")).toBeTruthy()
  expect(screen.getByText("Reliability not yet established")).toBeTruthy()
  expect(screen.getByText("Stock close · Sep 18, 2026")).toBeTruthy()
  expect(screen.queryByText(/ATM/)).toBeNull()
})
