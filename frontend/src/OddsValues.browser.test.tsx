// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it } from "vitest"

import OddsValues from "./OddsValues"
import type { PredictiveOdds } from "./marketOdds"

afterEach(cleanup)

it("keeps the real-world forecast first without presenting sample support as confidence", () => {
  const predictive: PredictiveOdds = {
    status: "available", method: "empirical_scaled", itm_pct_tenths: 500,
    otm_pct_tenths: 500, atm_pct_tenths: 0, as_of_session: "2026-09-18", support: 400,
  }
  const { container } = render(<OddsValues odds={{ status: "available", itm_pct_tenths: 700, otm_pct_tenths: 300, bound_low_pct_tenths: 650, bound_high_pct_tenths: 750 }} predictiveOdds={predictive} compact />)
  expect(container.querySelector(".odds-physical")?.textContent).toContain("50.0% ITM")
  expect(container.querySelector(".odds-market")?.textContent).toContain("70.0% ITM")
  expect(container.querySelector(".odds-values")?.firstElementChild?.className).toBe("odds-physical")
  expect(screen.getByText("Reliability not yet established")).toBeTruthy()
  expect(container.textContent).not.toContain("support 400")
})
