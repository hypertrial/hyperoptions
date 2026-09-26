export type HoldoutVerdict = "lost" | "trailed" | "held" | "missing"

export const VERDICT_TEXT: Record<HoldoutVerdict, string> = {
  lost: "Lost money in holdout",
  trailed: "Trailed buy and hold in holdout",
  held: "Held up in holdout",
  missing: "No holdout result",
}

export function verdictClass(verdict: HoldoutVerdict): string {
  if (verdict === "lost") return "border-destructive/40 text-destructive"
  if (verdict === "trailed") return "border-primary/50 text-foreground"
  if (verdict === "held") return "border-positive/40 text-positive"
  return "border-border text-muted-foreground"
}

export function holdoutVerdict(
  testCagr: number | null | undefined,
  testBuyHold: number | null | undefined,
): HoldoutVerdict {
  if (testCagr == null || !Number.isFinite(testCagr)) return "missing"
  if (testCagr < 0) return "lost"
  if (testBuyHold == null || !Number.isFinite(testBuyHold) || testCagr >= testBuyHold) return "held"
  return "trailed"
}
