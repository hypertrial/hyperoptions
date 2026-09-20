export type ExactDecimal = {
  value: bigint
  scale: number
}

const TOKEN = /^[+-]?\d+(\.\d+)?$/

export function parseExactDecimal(raw: string): ExactDecimal | null {
  if (!raw) return null
  if (!TOKEN.test(raw)) return null
  const negative = raw.startsWith("-")
  const unsigned = raw.replace(/^[+-]/, "")
  const [whole, fraction = ""] = unsigned.split(".")
  const digits = BigInt(`${whole}${fraction}`)
  return {
    value: negative ? -digits : digits,
    scale: fraction.length,
  }
}

export function parseExactToken(raw: string, extras: RegExp): ExactDecimal | null {
  const text = raw.replace(extras, "").trim()
  if (!text) return null
  return parseExactDecimal(text)
}

export function compareScaled(
  left: number,
  leftScale: number,
  right: ExactDecimal,
): number {
  if (!Number.isFinite(left) || !Number.isInteger(left)) return Number.NaN
  const leftScaled = BigInt(left) * (10n ** BigInt(right.scale))
  const rightScaled = right.value * (10n ** BigInt(leftScale))
  if (leftScaled === rightScaled) return 0
  return leftScaled > rightScaled ? 1 : -1
}

export function meetsScaledMinimum(
  value: number | null | undefined,
  threshold: ExactDecimal | null,
  valueScale: number,
): boolean {
  if (threshold == null) return true
  if (value == null || !Number.isFinite(value) || !Number.isInteger(value)) return false
  return compareScaled(value, valueScale, threshold) >= 0
}

export function meetsScaledMaximum(
  value: number | null | undefined,
  threshold: ExactDecimal | null,
  valueScale: number,
): boolean {
  if (threshold == null) return true
  if (value == null || !Number.isFinite(value) || !Number.isInteger(value)) return false
  return compareScaled(value, valueScale, threshold) <= 0
}
