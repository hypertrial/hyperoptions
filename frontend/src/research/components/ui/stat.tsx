import { cn } from "../../lib/utils"

type StatProps = {
  label: string
  value: string
  hint?: string
  className?: string
}

export function Stat({ label, value, hint, className }: StatProps) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={cn("num text-foreground", className)}>{value}</dd>
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  )
}
