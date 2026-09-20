import { Radio } from "@base-ui/react/radio"
import { RadioGroup } from "@base-ui/react/radio-group"

import type { Moneyness, Side } from "./types"

type Option<T extends string> = { value: T; label: string }

type Props<T extends string> = {
  label: string
  value: T
  options: readonly Option<T>[]
  onChange: (value: T) => void
}

function Segmented<T extends string>({ label, value, options, onChange }: Props<T>) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs font-bold text-muted-foreground">{label}</span>
      <RadioGroup
        className="segmented"
        value={value}
        onValueChange={(next) => {
          if (typeof next === "string") onChange(next as T)
        }}
        aria-label={label}
      >
        {options.map((option) => (
          <Radio.Root
            key={option.value}
            value={option.value}
            className="selected:bg-card selected:text-foreground min-h-11 px-3 font-bold text-muted-foreground data-checked:bg-card data-checked:text-foreground"
          >
            {option.label}
          </Radio.Root>
        ))}
      </RadioGroup>
    </div>
  )
}

export function StrategyToggle({ value, onChange }: { value: Side; onChange: (value: Side) => void }) {
  return (
    <Segmented
      label="Strategy"
      value={value}
      onChange={onChange}
      options={[
        { value: "call", label: "Covered calls" },
        { value: "put", label: "Cash-secured puts" },
      ]}
    />
  )
}

export function MoneynessToggle({ value, onChange }: { value: Moneyness; onChange: (value: Moneyness) => void }) {
  return (
    <Segmented
      label="Moneyness"
      value={value}
      onChange={onChange}
      options={[
        { value: "itm", label: "ITM" },
        { value: "otm", label: "OTM" },
        { value: "all", label: "All" },
      ]}
    />
  )
}
