export function formatPct(value: number | null | undefined, digits = 1): string {
  if (value == null || Number.isNaN(value)) return "—"
  return `${(value * 100).toFixed(digits)}%`
}

export function formatSignedPct(value: number | null | undefined, digits = 1): string {
  if (value == null || Number.isNaN(value)) return "—"
  const text = formatPct(value, digits)
  return value > 0 ? `+${text}` : text
}

export function formatNum(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return "—"
  return value.toFixed(digits)
}

export function formatInt(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—"
  return Math.round(value).toLocaleString("en-US")
}

export function tone(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value) || value === 0) return "text-foreground"
  return value > 0 ? "text-positive" : "text-destructive"
}
