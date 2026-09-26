import type { ReactNode } from "react"
import { cn } from "../../lib/utils"

type NoticeProps = {
  tone?: "info" | "success" | "warn" | "error"
  children: ReactNode
  onDismiss?: () => void
}

const tones = {
  info: "border-border text-foreground",
  success: "border-positive/40 text-foreground",
  warn: "border-primary/50 text-foreground",
  error: "border-destructive/40 text-destructive",
}

export function Alert({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="rounded-md border border-destructive/40 bg-card px-3 py-2 text-sm text-destructive">
      {children}
    </p>
  )
}

export function Notice({ tone = "info", children, onDismiss }: NoticeProps) {
  return (
    <div className={cn("flex items-start justify-between gap-3 border-t px-4 py-2 text-sm sm:px-6", tones[tone])} role={tone === "error" ? "alert" : "status"}>
      <p>{children}</p>
      {onDismiss ? (
        <button type="button" className="inline-flex min-h-6 items-center text-xs text-muted-foreground hover:text-foreground" onClick={onDismiss}>
          Dismiss
        </button>
      ) : null}
    </div>
  )
}
