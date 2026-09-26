import { Button } from "@/components/ui/button"

export type WatchActionState =
  | { phase: "pending" }
  | { phase: "added" }
  | { phase: "existing" }
  | { phase: "error"; message: string }

type Props = {
  contractLabel: string
  watchKey: string | null | undefined
  watchabilityReason: string | null | undefined
  state: WatchActionState | undefined
  mobile?: boolean
  onWatch: (watchKey: string) => void
}

export default function WatchButton({ contractLabel, watchKey, watchabilityReason, state, mobile = false, onWatch }: Props) {
  const unavailable = !watchKey
  const done = state?.phase === "added" || state?.phase === "existing"
  const pending = state?.phase === "pending"
  const reason = watchabilityReason || "This contract cannot be watched."
  const text = pending ? "Adding…" : state?.phase === "added" ? "Watching" : state?.phase === "existing" ? "Already watching" : state?.phase === "error" ? "Retry watch" : "Watch"
  return (
    <div className={mobile ? "watch-action watch-action-mobile" : "watch-action"}>
      <Button
        type="button"
        variant="outline"
        size={mobile ? "default" : "xs"}
        className="watch-row-button"
        disabled={unavailable || done || pending}
        title={unavailable ? reason : undefined}
        aria-label={`${text} ${contractLabel}${unavailable ? `: ${reason}` : ""}`}
        onClick={() => { if (watchKey) onWatch(watchKey) }}
      >
        {text}
      </Button>
      {unavailable ? <small className="watch-action-note">{reason}</small> : null}
      {state?.phase === "error" ? <small className="watch-action-note" role="alert">{state.message}</small> : null}
    </div>
  )
}
