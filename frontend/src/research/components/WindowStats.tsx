import type { ReactNode } from "react"
import { formatInt, formatNum, formatSignedPct, tone } from "../lib/format"
import { VERDICT_TEXT, holdoutVerdict, verdictClass } from "../lib/verdict"
import { Badge } from "./ui/badge"

export type WindowNumbers = {
  cagr: number | null | undefined
  buyHold: number | null | undefined
  drawdown: number | null | undefined
  trades: number | null | undefined
}

export function WindowStats({
  validation,
  holdout,
}: {
  validation: WindowNumbers
  holdout: WindowNumbers
}) {
  const verdict = holdoutVerdict(holdout.cagr, holdout.buyHold)
  return (
    <div className="@container mt-4">
      <div className="grid gap-4 @min-[32rem]:grid-cols-2">
        <Window title="Validation (ranked)" numbers={validation} />
        <Window title="Holdout (not ranked)" numbers={holdout} note={VERDICT_TEXT[verdict]} tone={verdictClass(verdict)} />
      </div>
    </div>
  )
}

function Window({ title, numbers, note, tone: noteTone }: { title: string; numbers: WindowNumbers; note?: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-border bg-muted/60 p-3">
      <h3 className="text-sm text-muted-foreground">{title}</h3>
      <dl className="mt-2 grid grid-cols-2 gap-2 text-sm">
        <Metric label="Strategy CAGR" value={formatSignedPct(numbers.cagr)} className={tone(numbers.cagr)} />
        <Metric label="Buy & hold" value={formatSignedPct(numbers.buyHold)} className={tone(numbers.buyHold)} />
        <Metric label="Max DD" value={formatSignedPct(numbers.drawdown)} className={tone(numbers.drawdown)} />
        <Metric label="Trades" value={formatInt(numbers.trades)} />
      </dl>
      {note ? <Badge className={`mt-3 max-w-full leading-snug ${noteTone ?? ""}`}>{note}</Badge> : null}
    </div>
  )
}

function Metric({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={className ? `num ${className}` : "num"}>{value}</dd>
    </div>
  )
}

export function RuleLines({
  entry,
  exit,
  filters = [],
}: {
  entry?: string[]
  exit?: string[]
  filters?: string[]
}) {
  const entrySignals = entry ?? []
  const exitSignals = exit ?? []
  const filterSignals = filters ?? []
  return (
    <div className="mt-2 space-y-1 text-sm text-foreground">
      <p>Enter when all of {entrySignals.length ? entrySignals.join(", ") : "—"}</p>
      {filterSignals.length ? <p>Filters {filterSignals.join(", ")}</p> : null}
      <p>Exit when any of {exitSignals.length ? exitSignals.join(", ") : "—"}</p>
    </div>
  )
}

export function Score({ value }: { value: number | null | undefined }) {
  return <p className="num text-3xl">{formatNum(value, 1)}</p>
}

export function Quiet({ children }: { children: ReactNode }) {
  return <p className="text-xs text-muted-foreground">{children}</p>
}
