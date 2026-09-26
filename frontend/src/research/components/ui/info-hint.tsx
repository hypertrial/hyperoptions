import * as Tooltip from "@radix-ui/react-tooltip"

type InfoHintProps = {
  label: string
  text: string
}

export function InfoHint({ label, text }: InfoHintProps) {
  return (
    <Tooltip.Provider delayDuration={200}>
      <Tooltip.Root>
        <Tooltip.Trigger asChild>
          <button type="button" className="inline-flex min-h-6 items-center text-xs text-muted-foreground underline decoration-dotted underline-offset-2" aria-label={label}>
            {label}
          </button>
        </Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content className="z-20 max-w-xs rounded-md border border-border bg-card px-2 py-1 text-xs text-foreground shadow-sm" sideOffset={6}>
            {text}
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  )
}
