import { Monitor, Moon, Sun } from "lucide-react"

import { Button } from "@/components/ui/button"
import { useTheme } from "./theme"

export default function ThemeToggle() {
  const { preference, cycle } = useTheme()
  const label = preference === "system" ? "Theme: system" : preference === "dark" ? "Theme: dark" : "Theme: light"
  const Icon = preference === "system" ? Monitor : preference === "dark" ? Moon : Sun
  return (
    <Button type="button" variant="ghost" size="icon" aria-label={label} onClick={cycle}>
      <Icon aria-hidden="true" />
    </Button>
  )
}
