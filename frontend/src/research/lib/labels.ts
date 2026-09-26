export type Gates = {
  minTrades: number
  minValTrades: number
  maxDrawdown: number
  minDegradation: number
  minStability: number
}

export const defaultGates: Gates = {
  minTrades: 20,
  minValTrades: 5,
  maxDrawdown: 0.6,
  minDegradation: 0.4,
  minStability: 0.5,
}

export const FAMILIES = [
  "trend_following",
  "momentum_pullback",
  "momentum_continuation",
  "volume_pullback",
  "volume_continuation",
  "volatility_fade",
  "volatility_breakout",
  "trend_momentum",
  "trend_momentum_volume",
  "trend_volatility",
  "momentum_volatility",
  "trend_momentum_volatility",
  "trend_momentum_volume_volatility",
] as const

export const EXITS = ["mirror", "atr_trail", "time"] as const

export const FLAGS = [
  "insufficient_trades",
  "extreme_drawdown",
  "severe_degradation",
  "unstable_parameters",
  "no_neighbours",
] as const

export const SEGMENTS = ["train", "validation", "test", "full"] as const

const FAMILY_LABELS: Record<(typeof FAMILIES)[number], string> = {
  trend_following: "Trend following",
  momentum_pullback: "Momentum pullback",
  momentum_continuation: "Momentum continuation",
  volume_pullback: "Volume pullback",
  volume_continuation: "Volume continuation",
  volatility_fade: "Volatility fade",
  volatility_breakout: "Volatility breakout",
  trend_momentum: "Trend + momentum",
  trend_momentum_volume: "Trend + momentum + volume",
  trend_volatility: "Trend + volatility",
  momentum_volatility: "Momentum + volatility",
  trend_momentum_volatility: "Trend + momentum + volatility",
  trend_momentum_volume_volatility: "Trend + momentum + volume + volatility",
}

const SEGMENT_LABELS: Record<(typeof SEGMENTS)[number], string> = {
  train: "Train",
  validation: "Validation",
  test: "Holdout",
  full: "Full",
}

export const METRICS = {
  robustness: {
    label: "Robustness",
    hint: "Score from the validation window and walk-forward folds. The holdout is not an input.",
  },
  validationCagr: {
    label: "Validation CAGR",
    hint: "Compound annual growth on the validation window, which is part of the score.",
  },
  holdoutCagr: {
    label: "Holdout CAGR",
    hint: "Compound annual growth on the final test window. It is shown and never used to rank.",
  },
  sharpe: { label: "Sharpe", hint: "Sharpe ratio on the validation window." },
  maxDrawdown: { label: "Max DD", hint: "Worst peak-to-trough decline on that window." },
  winRate: { label: "Win rate", hint: "Share of trades that made money on the validation window." },
  trades: { label: "Trades", hint: "Closed trades on that window." },
  buyHoldCagr: { label: "Buy & hold CAGR", hint: "Buy and hold on the same window as the strategy figure beside it." },
} as const

const EXIT_LABELS: Record<(typeof EXITS)[number], string> = {
  mirror: "Mirror exit",
  atr_trail: "ATR trail",
  time: "Time stop",
}

export function exitLabel(kind: string): string {
  if (kind in EXIT_LABELS) return EXIT_LABELS[kind as keyof typeof EXIT_LABELS]
  return kind.replaceAll("_", " ")
}

export function familyLabel(family: string): string {
  if (family in FAMILY_LABELS) return FAMILY_LABELS[family as keyof typeof FAMILY_LABELS]
  return family.replaceAll("_", " ")
}

export function segmentLabel(segment: string): string {
  if (segment in SEGMENT_LABELS) return SEGMENT_LABELS[segment as keyof typeof SEGMENT_LABELS]
  return segment
}

export function gatesFromConfig(raw: {
  min_trades: number
  min_val_trades: number
  max_drawdown: number
  min_degradation: number
  min_stability: number
}): Gates {
  return {
    minTrades: raw.min_trades,
    minValTrades: raw.min_val_trades,
    maxDrawdown: raw.max_drawdown,
    minDegradation: raw.min_degradation,
    minStability: raw.min_stability,
  }
}

export function flagLabel(flag: string, gates: Gates = defaultGates): string {
  const drawdown = Math.round(gates.maxDrawdown * 100)
  switch (flag) {
    case "insufficient_trades":
      return `Fewer than ${gates.minTrades} trades, or fewer than ${gates.minValTrades} validation trades`
    case "extreme_drawdown":
      return `Validation drawdown worse than ${drawdown}%`
    case "severe_degradation":
      return `Validation Sharpe fell below ${gates.minDegradation} of train Sharpe`
    case "unstable_parameters":
      return `Parameter stability below ${gates.minStability}`
    case "no_neighbours":
      return "No parameter neighbours in this batch"
    default:
      return flag.replaceAll("_", " ")
  }
}
