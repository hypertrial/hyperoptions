const dollars = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
})

export function moneyCents(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || !Number.isInteger(value)) return "—"
  const sign = value < 0 ? "-" : ""
  const abs = Math.abs(value)
  const whole = Math.trunc(abs / 100)
  const frac = abs % 100
  const formatted = `${dollars.format(whole)}.${String(frac).padStart(2, "0")}`
  if (!sign) return formatted
  return formatted.startsWith("-") ? formatted : `-${formatted}`
}

export function moneyStrike(exact: string | null | undefined, cents: number): string {
  return exact ? `$${exact}` : moneyCents(cents)
}

export function percentTenths(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || !Number.isInteger(value)) return "—"
  const sign = value > 0 ? "+" : value < 0 ? "-" : ""
  const abs = Math.abs(value)
  return `${sign}${Math.trunc(abs / 10)}.${abs % 10}%`
}

export function unsignedPercentTenths(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || !Number.isInteger(value)) return "—"
  const sign = value < 0 ? "-" : ""
  const abs = Math.abs(value)
  return `${sign}${Math.trunc(abs / 10)}.${abs % 10}%`
}

export function integer(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : Math.trunc(value).toLocaleString("en-US")
}

const fetchedAtFormat = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  hour: "numeric",
  minute: "2-digit",
  timeZoneName: "short",
})

export function plural(count: number, singular: string): string {
  return count === 1 ? singular : `${singular}s`
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return "—"
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? value : fetchedAtFormat.format(parsed)
}
