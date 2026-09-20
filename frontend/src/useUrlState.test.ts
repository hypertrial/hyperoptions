import { describe, expect, it } from "vitest"

import { defaultMoneyness, parseUrlState, serializeUrlState } from "./useUrlState"

describe("url state", () => {
  it("defaults to IREN covered-call ITM with hidden column overrides", () => {
    const state = parseUrlState("")
    expect(state).toEqual({ ticker: "IREN", side: "call", moneyness: "itm", cols: null })
    expect(defaultMoneyness("put")).toBe("otm")
  })

  it("accepts ticker, strategy, moneyness, and columns", () => {
    const state = parseUrlState("?t=cifr&side=put&m=all&cols=strike_cents,iv_pct_tenths")
    expect(state).toEqual({
      ticker: "CIFR",
      side: "put",
      moneyness: "all",
      cols: ["strike_cents", "iv_pct_tenths"],
    })
    expect(serializeUrlState(state)).toBe("?t=CIFR&side=put&m=all&cols=strike_cents%2Civ_pct_tenths")
  })

  it("rejects invalid tickers and omits default columns from the query", () => {
    expect(parseUrlState("?t=not-a-ticker").ticker).toBe("IREN")
    expect(serializeUrlState({ ticker: "IREN", side: "call", moneyness: "itm", cols: null }))
      .toBe("?t=IREN&side=call&m=itm")
  })

  it("deduplicates columns and removes IDs unavailable for the selected strategy", () => {
    expect(parseUrlState("?side=call&cols=strike_cents,strike_cents,unknown").cols)
      .toEqual(["strike_cents"])
    expect(parseUrlState("?side=put&cols=call_bid_cents").cols).toBeNull()
  })
})
