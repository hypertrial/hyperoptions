import { describe, expect, it } from "vitest"
import { VERDICT_TEXT, holdoutVerdict, verdictClass } from "./verdict"

describe("holdoutVerdict", () => {
  it("names each outcome", () => {
    expect(holdoutVerdict(-0.4, 0.5)).toBe("lost")
    expect(holdoutVerdict(0.1, 0.5)).toBe("trailed")
    expect(holdoutVerdict(0.5, 0.2)).toBe("held")
    expect(holdoutVerdict(null, 0.2)).toBe("missing")
  })

  it("treats zero as not a loss and a tie with buy and hold as held up", () => {
    expect(holdoutVerdict(0, -0.1)).toBe("held")
    expect(holdoutVerdict(0.2, 0.2)).toBe("held")
    expect(holdoutVerdict(0, 0)).toBe("held")
    expect(VERDICT_TEXT.lost).toBe("Lost money in holdout")
    expect(verdictClass("lost")).toContain("text-destructive")
    expect(verdictClass("trailed")).toContain("text-foreground")
    expect(verdictClass("held")).toContain("text-positive")
    expect(verdictClass("missing")).toContain("text-muted-foreground")
    expect(VERDICT_TEXT.missing).not.toMatch(/ranked/i)
  })

  it("treats missing and non-finite values as no result", () => {
    expect(holdoutVerdict(undefined, 1)).toBe("missing")
    expect(holdoutVerdict(Number.NaN, 1)).toBe("missing")
    expect(holdoutVerdict(Number.POSITIVE_INFINITY, 1)).toBe("missing")
    expect(holdoutVerdict(0.1, null)).toBe("held")
  })
})
