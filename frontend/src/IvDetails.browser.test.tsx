// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it } from "vitest"

import IvDetails from "./IvDetails"
import { sampleContract, sampleIvDetails } from "./testFixtures"

afterEach(cleanup)

it("uses a native disclosure with contract-specific naming and exact input facts", () => {
  const row = sampleContract({ iv_pct_tenths: 877, iv_details: sampleIvDetails() })
  const { container } = render(<IvDetails row={row} contractLabel="IREN 2026-09-18 call strike $80.00" />)
  const summary = screen.getByText("Details", { exact: false, selector: "summary" })
  expect(summary.getAttribute("aria-label")).toBe("IV details for IREN 2026-09-18 call strike $80.00: 87.7% · Details")
  expect(container.querySelector("details")?.open).toBe(false)
  expect(container.querySelector("summary")?.textContent).toBe("87.7% · Details")
  expect(screen.getByText("72.1%–97.1%")).toBeTruthy()
  expect(screen.getByText("$100.00")).toBeTruthy()
  expect(screen.getByText("0.01917808219178082191780821918")).toBeTruthy()
  expect(screen.getByText(/not a statistical confidence interval/)).toBeTruthy()
  expect(screen.getByText(/approximation for American equity options/)).toBeTruthy()
})

it("shows endpoint results when midpoint is unavailable and omits an incomplete range", () => {
  const row = sampleContract({ iv_pct_tenths: null, iv_details: sampleIvDetails({
    status: "unavailable",
    reason: { code: "midpoint_eligibility", message: "Midpoint has insufficient extrinsic value." },
    bid_pct_tenths: null,
    bid_reason: { code: "model_bounds", message: "Bid lies outside model bounds." },
  }) })
  const { container } = render(<IvDetails row={row} contractLabel="IREN 2026-09-18 put strike $80.00" />)
  expect(container.querySelector("summary")?.textContent).toBe("— · Why unavailable?")
  expect(screen.getByText("Unavailable — Midpoint has insufficient extrinsic value.")).toBeTruthy()
  expect(screen.getByText("Unavailable — Bid lies outside model bounds.")).toBeTruthy()
  expect(screen.getByText("97.1%")).toBeTruthy()
  expect(screen.queryByText("Quote-implied IV range")).toBeNull()
})

it("keeps Details for an available midpoint with a zero bid and a valid ask", () => {
  const row = sampleContract({ iv_pct_tenths: 877, iv_details: sampleIvDetails({
    bid_price_exact: "0",
    bid_pct_tenths: null,
    bid_reason: { code: "model_bounds", message: "Bid must be positive and inside model bounds." },
  }) })
  const { container } = render(<IvDetails row={row} contractLabel="IREN call strike $80.00" />)
  expect(container.querySelector("summary")?.textContent).toBe("87.7% · Details")
  expect(screen.getByText("Unavailable — Bid must be positive and inside model bounds.")).toBeTruthy()
  expect(screen.getByText("97.1%")).toBeTruthy()
  expect(screen.queryByText("Quote-implied IV range")).toBeNull()
})

it("explains missing diagnostics without substituting rounded table inputs", () => {
  const { container } = render(<IvDetails row={sampleContract()} contractLabel="IREN call strike $50.00" />)
  expect(container.querySelector("summary")?.textContent).toBe("45.0% · Details")
  expect(screen.getByText("Calculation details unavailable.")).toBeTruthy()
  expect(screen.queryByText("Pricing spot")).toBeNull()
  expect(screen.queryByText("$0.50")).toBeNull()
})
