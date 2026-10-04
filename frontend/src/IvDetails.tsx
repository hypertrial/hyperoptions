import { useId, useRef } from "react"

import { Button } from "./components/ui/button"
import { unsignedPercentTenths } from "./format"
import { IV_DETAILS_UNAVAILABLE, IV_MODEL_NOTE, IV_SENSITIVITY_NOTE, ivCalculationFacts, type IvRow } from "./ivFacts"

export default function IvDetails({ row, contractLabel, inline = false }: { row: IvRow; contractLabel: string; inline?: boolean }) {
  const id = useId()
  const disclosure = useRef<HTMLDetailsElement>(null)
  const summary = useRef<HTMLElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const usePopover = !inline && typeof HTMLElement !== "undefined" && "showPopover" in HTMLElement.prototype
  const close = () => {
    if (panel.current?.matches(":popover-open")) panel.current.hidePopover()
    if (disclosure.current) disclosure.current.open = false
    summary.current?.focus()
  }
  const available = row.iv_pct_tenths != null
  const pending = row.iv_details?.status === "pending"
  const value = unsignedPercentTenths(row.iv_pct_tenths)
  const action = pending ? "Loading inputs…" : available ? "Details" : "Why unavailable?"
  return (
    <details ref={disclosure} className={`iv-details${usePopover ? "" : " iv-details-inline"}`} onToggle={(event) => {
      if (!usePopover || event.target !== event.currentTarget || !panel.current?.isConnected) return
      if (event.currentTarget.open && !panel.current.matches(":popover-open")) panel.current.showPopover({ source: summary.current ?? undefined })
      else if (!event.currentTarget.open && panel.current.matches(":popover-open")) panel.current.hidePopover()
    }}>
      <summary ref={summary} aria-controls={id} aria-label={`IV details for ${contractLabel}: ${value} · ${action}`}>
        <span className="iv-midpoint font-mono">{value}</span> · {action}
      </summary>
      <div ref={panel} id={id} className="iv-details-content" popover={usePopover ? "auto" : undefined}
        role={usePopover ? "region" : undefined} aria-label={usePopover ? `IV calculation for ${contractLabel}` : undefined}
        tabIndex={usePopover ? 0 : undefined}
        onBeforeToggle={(event) => {
          // Dismiss synchronously: a queued close must not collapse a freshly reopened disclosure.
          if (usePopover && event.target === event.currentTarget && event.newState === "closed" && disclosure.current) disclosure.current.open = false
        }}
        onKeyDown={(event) => {
          if (usePopover && event.key === "Escape") { event.preventDefault(); close() }
        }}>
        {usePopover && (
          <div className="iv-panel-header">
            <div><h3>IV calculation</h3><p>{contractLabel}</p></div>
            <Button variant="outline" size="sm" onClick={close}>Close</Button>
          </div>
        )}
        {row.iv_details ? (
          <>
            <p>{IV_MODEL_NOTE}</p>
            <p>{IV_SENSITIVITY_NOTE}</p>
            <dl className="iv-facts">
              {ivCalculationFacts(row).map(({ label, value }) => (
                <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
              ))}
            </dl>
          </>
        ) : <p>{IV_DETAILS_UNAVAILABLE}</p>}
      </div>
    </details>
  )
}
