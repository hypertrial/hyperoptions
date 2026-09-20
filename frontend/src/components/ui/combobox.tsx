import { Combobox as ComboboxPrimitive } from "@base-ui/react"

import { Input } from "@/components/ui/input"

const Combobox = ComboboxPrimitive.Root

function ComboboxInput({
  className,
  disabled = false,
  ...props
}: ComboboxPrimitive.Input.Props) {
  return (
    <ComboboxPrimitive.Input
      render={<Input disabled={disabled} />}
      className={className}
      disabled={disabled}
      {...props}
    />
  )
}

export { Combobox, ComboboxInput }
