import { describe, expect, it } from "vitest"

import { defaultMoneyness, parseUrlState, serializeUrlState } from "./useUrlState"

describe("url state", () => {
  it("defaults to IREN covered-call ITM", () => {
    const state = parseUrlState("")
    expect(state).toEqual({ ticker: "IREN", side: "call", moneyness: "itm" })
    expect(defaultMoneyness("put")).toBe("otm")
    expect(defaultMoneyness("call")).toBe("itm")
  })

  it("accepts ticker, strategy, and moneyness, and drops retired column and all-moneyness params", () => {
    const state = parseUrlState("?t=cifr&side=put&m=otm&cols=strike_cents,iv_pct_tenths")
    expect(state).toEqual({ ticker: "CIFR", side: "put", moneyness: "otm" })
    expect(serializeUrlState(state)).toBe("?t=CIFR&side=put&m=otm")
    expect(parseUrlState("?side=put&m=all")).toEqual({ ticker: "IREN", side: "put", moneyness: "otm" })
    expect(parseUrlState("?side=call&m=all")).toEqual({ ticker: "IREN", side: "call", moneyness: "itm" })
  })

  it("rejects invalid tickers", () => {
    expect(parseUrlState("?t=not-a-ticker").ticker).toBe("IREN")
    expect(serializeUrlState({ ticker: "IREN", side: "call", moneyness: "itm" }))
      .toBe("?t=IREN&side=call&m=itm")
  })
})
