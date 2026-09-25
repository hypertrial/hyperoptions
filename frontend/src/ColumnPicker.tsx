import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { defaultColumnIds, normalizeColumnIds, strategyColumns } from "./columns"
import type { Side } from "./types"

const GROUPS = [
  { id: "market", label: "Market & liquidity" },
  { id: "capital", label: "Capital" },
  { id: "returns", label: "Returns" },
  { id: "risk", label: "Breakeven & risk" },
  { id: "history", label: "Historical lows" },
  { id: "greeks", label: "Greeks" },
] as const

type Props = {
  side: Side
  selected: string[] | null
  onChange: (ids: string[] | null) => void
}

export default function ColumnPicker({ side, selected, onChange }: Props) {
  const columns = strategyColumns(side)
  const visible = new Set(normalizeColumnIds(side, selected) ?? defaultColumnIds(side))

  const toggle = (id: string, checked: boolean) => {
    if (!checked && visible.has(id) && visible.size === 1) return
    const next = columns.map((column) => column.id).filter((columnId) => (
      columnId === id ? checked : visible.has(columnId)
    ))
    const defaults = defaultColumnIds(side)
    const same = next.length === defaults.length && next.every((item, index) => item === defaults[index])
    onChange(same ? null : next)
  }

  return (
    <Popover>
      <PopoverTrigger
        nativeButton
        className="secondary-button inline-flex items-center gap-2"
      >
        Columns
        <Badge variant="secondary">{visible.size}</Badge>
      </PopoverTrigger>
      <PopoverContent className="column-picker-popover w-[min(40rem,calc(100vw-2rem))]">
        <div className="column-picker-actions">
          <div>
            <strong>Visible columns</strong>
            <p>Choose the metrics that support this decision.</p>
          </div>
          <div className="inline-flex flex-wrap gap-2">
            <Button type="button" variant="outline" onClick={() => onChange(null)}>
              Reset to default
            </Button>
            <Button type="button" variant="outline" onClick={() => onChange(columns.map((column) => column.id))}>
              Show all
            </Button>
          </div>
        </div>
        <div className="column-picker-body">
          {GROUPS.map((group) => {
            const grouped = columns.filter((column) => column.group === group.id)
            if (grouped.length === 0) return null
            return (
              <fieldset key={group.id}>
                <legend>{group.label}</legend>
                {grouped.map((column) => (
                  <label key={column.id} className="column-option">
                    <Checkbox
                      checked={visible.has(column.id)}
                      onCheckedChange={(checked) => toggle(column.id, checked === true)}
                    />
                    <span>{column.label}</span>
                  </label>
                ))}
              </fieldset>
            )
          })}
        </div>
      </PopoverContent>
    </Popover>
  )
}
