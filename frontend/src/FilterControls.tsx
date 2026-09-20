import { AlignJustify, ChevronDown, ChevronsDownUp, ChevronsUpDown, Filter, Rows3, X } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import ColumnPicker from "./ColumnPicker"
import type { Density } from "./density"
import type { ExactDecimal } from "./decimal"
import type { Side } from "./types"

type Props = {
  side: Side
  selectedColumns: string[] | null
  density: Density
  visibleCount: number
  expirationCount: number
  expandedCount: number
  minPrimaryText: string
  minAprText: string
  minDropText: string
  minDteText: string
  maxDteText: string
  minPrimary: ExactDecimal | null
  minApr: ExactDecimal | null
  minDrop: ExactDecimal | null
  minDte: ExactDecimal | null
  maxDte: ExactDecimal | null
  invertedDte: boolean
  onChangeColumns: (ids: string[] | null) => void
  onToggleDensity: () => void
  onExpandAll: () => void
  onCollapseAll: () => void
  onMinPrimaryChange: (value: string) => void
  onMinAprChange: (value: string) => void
  onMinDropChange: (value: string) => void
  onMinDteChange: (value: string) => void
  onMaxDteChange: (value: string) => void
  onClearFilters: () => void
}

type FilterFieldProps = {
  id: string
  label: string
  value: string
  parsed: ExactDecimal | null
  invalidRange?: boolean
  onChange: (value: string) => void
}

function FilterField({ id, label, value, parsed, invalidRange = false, onChange }: FilterFieldProps) {
  const invalidToken = value.trim() !== "" && parsed == null
  const invalid = invalidToken || invalidRange
  const errorId = `${id}-error`
  return (
    <div className="filter-field">
      <Label className="control-field" htmlFor={id}>
        <span>{label}</span>
        <Input
          id={id}
          type="text"
          inputMode="decimal"
          autoComplete="off"
          className="border-0 bg-transparent shadow-none focus-visible:border-0 focus-visible:ring-0"
          placeholder="any"
          value={value}
          aria-invalid={invalid}
          aria-describedby={invalid ? errorId : undefined}
          onChange={(event) => onChange(event.target.value)}
        />
      </Label>
      {invalid ? (
        <p id={errorId} className="field-error">
          {invalidRange ? "Minimum DTE must not exceed maximum DTE." : "Enter a valid number."}
        </p>
      ) : null}
    </div>
  )
}

export default function FilterControls({
  side,
  selectedColumns,
  density,
  visibleCount,
  expirationCount,
  expandedCount,
  minPrimaryText,
  minAprText,
  minDropText,
  minDteText,
  maxDteText,
  minPrimary,
  minApr,
  minDrop,
  minDte,
  maxDte,
  invertedDte,
  onChangeColumns,
  onToggleDensity,
  onExpandAll,
  onCollapseAll,
  onMinPrimaryChange,
  onMinAprChange,
  onMinDropChange,
  onMinDteChange,
  onMaxDteChange,
  onClearFilters,
}: Props) {
  const primaryLabel = side === "put" ? "Min Premium ($)" : "Min Called P&L ($)"
  const aprLabel = "Min APR net (%)"
  const dropLabel = side === "put" ? "Min Cushion to breakeven (%)" : "Min Drop to breakeven (%)"
  const chips = [
    { key: "primary", label: primaryLabel.replace(/^Min /, ""), value: minPrimaryText, clear: () => onMinPrimaryChange("") },
    { key: "apr", label: aprLabel.replace(/^Min /, ""), value: minAprText, clear: () => onMinAprChange("") },
    { key: "drop", label: dropLabel.replace(/^Min /, ""), value: minDropText, clear: () => onMinDropChange("") },
    { key: "min-dte", label: "DTE ≥", value: minDteText, clear: () => onMinDteChange("") },
    { key: "max-dte", label: "DTE ≤", value: maxDteText, clear: () => onMaxDteChange("") },
  ].filter((chip) => chip.value.trim() !== "")

  return (
    <Collapsible defaultOpen={false} className="results-controls">
      <div className="results-toolbar">
        <p className="results-count" aria-live="polite">
          <strong>{visibleCount.toLocaleString("en-US")}</strong> contracts
          <span aria-hidden="true"> · </span>
          <strong>{expirationCount.toLocaleString("en-US")}</strong> expirations
        </p>
        <div className="toolbar-actions">
          <CollapsibleTrigger className="secondary-button toolbar-button">
            <Filter aria-hidden="true" />
            Filters
            {chips.length > 0 ? <Badge variant="secondary">{chips.length}</Badge> : null}
            <ChevronDown className="chevron" aria-hidden="true" />
          </CollapsibleTrigger>
          <ColumnPicker side={side} selected={selectedColumns} onChange={onChangeColumns} />
          <Button
            type="button"
            variant="outline"
            className="density-button"
            aria-label={density === "compact" ? "Density: compact" : "Density: comfortable"}
            onClick={onToggleDensity}
          >
            {density === "compact" ? <AlignJustify aria-hidden="true" /> : <Rows3 aria-hidden="true" />}
            Density
          </Button>
          <Button type="button" variant="outline" onClick={onExpandAll} disabled={expirationCount === 0 || expandedCount === expirationCount}>
            <ChevronsUpDown aria-hidden="true" />
            Expand all
          </Button>
          <Button type="button" variant="outline" onClick={onCollapseAll} disabled={expandedCount === 0}>
            <ChevronsDownUp aria-hidden="true" />
            Collapse all
          </Button>
        </div>
      </div>

      {chips.length > 0 ? (
        <div className="filter-chips" aria-label="Active filters">
          {chips.map((chip) => (
            <button key={chip.key} type="button" className="filter-chip" onClick={chip.clear} aria-label={`Remove filter ${chip.label} ${chip.value}`}>
              <span>{chip.label} {chip.value}</span>
              <X aria-hidden="true" />
            </button>
          ))}
          <Button type="button" variant="ghost" size="sm" onClick={onClearFilters}>Clear all</Button>
        </div>
      ) : null}

      <CollapsibleContent keepMounted className="filter-panel">
        <div className="filter-cluster">
          <FilterField id="min-primary" label={primaryLabel} value={minPrimaryText} parsed={minPrimary} onChange={onMinPrimaryChange} />
          <FilterField id="min-apr" label={aprLabel} value={minAprText} parsed={minApr} onChange={onMinAprChange} />
          <FilterField id="min-drop" label={dropLabel} value={minDropText} parsed={minDrop} onChange={onMinDropChange} />
          <FilterField id="min-dte" label="Min DTE" value={minDteText} parsed={minDte} invalidRange={invertedDte} onChange={onMinDteChange} />
          <FilterField id="max-dte" label="Max DTE" value={maxDteText} parsed={maxDte} invalidRange={invertedDte} onChange={onMaxDteChange} />
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}
