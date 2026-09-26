const easternClock = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  weekday: "short",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
})

export function isRegularMarketHours(now = new Date()): boolean {
  const parts = Object.fromEntries(easternClock.formatToParts(now).map(({ type, value }) => [type, value]))
  if (parts.weekday === "Sat" || parts.weekday === "Sun") return false
  const minutes = Number(parts.hour) * 60 + Number(parts.minute)
  return minutes >= 9 * 60 + 30 && minutes < 16 * 60
}
