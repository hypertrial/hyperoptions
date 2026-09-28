export const PHYSICAL_MODELS = [
  "lognormal_ewma",
  "empirical_scaled",
  "student_t_ewma",
  "gjr_garch_t",
  "ohlc_har",
  "skew_t_ewma",
  "egarch_skew_t",
  "markov_switching",
  "ngboost_pooled",
  "earnings_jump",
  "iv_physical",
  "intraday_shadow",
] as const

export type PhysicalModel = typeof PHYSICAL_MODELS[number]
export const DEFAULT_FORECAST_MODEL: PhysicalModel = "lognormal_ewma"
export const FORECAST_MODEL_KEY = "hyperoptions.forecastModel"

export const PHYSICAL_MODEL_NAMES: Record<PhysicalModel, string> = {
  lognormal_ewma: "EWMA lognormal",
  empirical_scaled: "Scaled empirical",
  student_t_ewma: "Student-t EWMA",
  gjr_garch_t: "GJR-GARCH Student-t",
  ohlc_har: "Daily OHLC range/HAR proxy",
  skew_t_ewma: "Skewed-t EWMA",
  egarch_skew_t: "EGARCH skewed-t",
  markov_switching: "Two-regime switching variance",
  ngboost_pooled: "Pooled NGBoost",
  earnings_jump: "Earnings jump",
  iv_physical: "IV-informed forecast",
  intraday_shadow: "Intraday conditioned",
}

export function physicalModel(value: unknown): PhysicalModel | null {
  return typeof value === "string" && PHYSICAL_MODELS.some((method) => method === value)
    ? value as PhysicalModel : null
}

export function physicalModelName(value: string | null | undefined): string {
  return physicalModel(value) ? PHYSICAL_MODEL_NAMES[value as PhysicalModel] : "Unknown model"
}

export function marketModelName(value: string | null | undefined): string {
  return value === "regimelib" ? "Regimelib" : value === "constrained_call_curve"
    ? "Constrained call curve" : value === "ssvi" ? "SSVI volatility surface" : "Unknown market model"
}
