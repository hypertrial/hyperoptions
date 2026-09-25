import { AlignJustify, ChevronDown, ChevronsDownUp, ChevronsUpDown, Filter, Rows3, X } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import ColumnPicker from "./ColumnPicker"
import type { Density } from "./density"
import type { ExactDecimal } from "./decimal"
import { filterFieldSpecs, type FilterId, type FilterState, type FilterTexts } from "./filters"
import { plural } from "./format"
import type { Side } from "./types"

type Props = {
  side: Side
  selectedColumns: string[] | null
  density: Density
  visibleCount: number
  expirationCount: number
  expandedCount: number
  texts: FilterTexts
  parsed: FilterState
  invertedDte: boolean
  onChangeColumns: (ids: string[] | null) => void
  onToggleDensity: () => void
  onExpandAll: () => void
  onCollapseAll: () => void
  onTextChange: (id: FilterId, value: string) => void
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
  texts,
  parsed,
  invertedDte,
  onChangeColumns,
  onToggleDensity,
  onExpandAll,
  onCollapseAll,
  onTextChange,
  onClearFilters,
}: Props) {
  const specs = filterFieldSpecs(side)
  const parsedById: Record<FilterId, ExactDecimal | null> = {
    primary: parsed.primary,
    apr: parsed.apr,
    drop: parsed.drop,
    minDte: parsed.minDte,
    maxDte: parsed.maxDte,
  }
  const chips = specs
    .map((spec) => ({
      key: spec.chipKey,
      label: spec.chipLabel,
      value: texts[spec.id],
      clear: () => onTextChange(spec.id, ""),
    }))
    .filter((chip) => chip.value.trim() !== "")

  return (
    <Collapsible defaultOpen={false} className="results-controls">
      <div className="results-toolbar">
        <p className="results-count" aria-live="polite">
          <strong>{visibleCount.toLocaleString("en-US")}</strong> {plural(visibleCount, "contract")}
          <span aria-hidden="true"> · </span>
          <strong>{expirationCount.toLocaleString("en-US")}</strong> {plural(expirationCount, "expiration")}
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
          {specs.map((spec) => (
            <FilterField
              key={spec.inputId}
              id={spec.inputId}
              label={spec.label}
              value={texts[spec.id]}
              parsed={parsedById[spec.id]}
              invalidRange={spec.rowKey === "dte" && invertedDte}
              onChange={(value) => onTextChange(spec.id, value)}
            />
          ))}
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}
