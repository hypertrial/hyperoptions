import { ArrowDown, ArrowUp, Check, ChevronRight, Copy } from "lucide-react"
import { Fragment, type CSSProperties, type ReactNode } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { mobilePriorityColumns, type ColumnDef, type SizedContract } from "./columns"
import { copyRowAccessibleName, copyRowStateKey } from "./copyRow"
import type { Density } from "./density"
import { integer, moneyStrike } from "./format"
import { heatmapHue, heatmapStop, type MetricRange } from "./heatmap"
import { oddsLabel } from "./marketOdds"
import OddsValues from "./OddsValues"
import type { Side } from "./types"
import { useMediaQuery } from "./useMediaQuery"
import type { SortState, VisibleGroup } from "./viewModel"
import WatchButton, { type WatchActionState } from "./watchlist/WatchButton"

function heatProps(value: number | null | undefined, range: MetricRange | null) {
  const stop = heatmapStop(value, range)
  if (stop == null) return {}
  return {
    className: "heat",
    "data-heat": stop.toFixed(2),
    style: { "--heat": String(heatmapHue(stop)) } as CSSProperties,
  }
}

function HeatCell({
  value,
  range,
  children,
  heading = false,
}: {
  value: number | null | undefined
  range: MetricRange | null
  children: ReactNode
  heading?: boolean
}) {
  const heat = heatProps(value, range)
  if (heading) return <th scope="row" {...heat}>{children}</th>
  return <td {...heat}>{children}</td>
}

type Props = {
  ticker: string
  side: Side
  columns: ColumnDef[]
  groups: VisibleGroup[]
  mountedGroups: VisibleGroup[]
  expandedExpirations: ReadonlySet<string>
  copiedKey: string | null
  sort: SortState
  density: Density
  onToggleExpiration: (expiration: string) => void
  onSort: (id: string) => void
  onCopy: (row: SizedContract, group: VisibleGroup["group"]) => void
  watchStates: Readonly<Record<string, WatchActionState>>
  onWatch: (watchKey: string) => void
}

function DesktopResults({
  ticker,
  columns,
  rows,
  group,
  ranges,
  copiedKey,
  sort,
  density,
  onSort,
  onCopy,
  watchStates,
  onWatch,
}: {
  ticker: string
  columns: ColumnDef[]
  rows: SizedContract[]
  group: VisibleGroup["group"]
  ranges: VisibleGroup["ranges"]
  copiedKey: string | null
  sort: SortState
  density: Density
  onSort: (id: string) => void
  onCopy: (row: SizedContract, group: VisibleGroup["group"]) => void
  watchStates: Readonly<Record<string, WatchActionState>>
  onWatch: (watchKey: string) => void
}) {
  return (
    <div className="table-scroll">
      <table data-density={density}>
        <thead>
          <tr>
            {columns.map((column, index) => {
              const active = sort.id === column.id
              const ariaSort = active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"
              return (
                <Fragment key={column.id}>
                  <th scope="col" title={column.info} aria-sort={ariaSort}>
                    <button type="button" onClick={() => onSort(column.id)}>
                      {column.abbrev ? <abbr title={column.info}>{column.label}</abbr> : column.label}
                      {active ? (sort.dir === "asc" ? <ArrowUp aria-hidden="true" /> : <ArrowDown aria-hidden="true" />) : null}
                    </button>
                  </th>
                  {index === 0 ? <th scope="col" title="Risk-neutral odds that the regular-session expiry close is in or out of the money">ITM / OTM odds</th> : null}
                </Fragment>
              )
            })}
            <th scope="col" title="Add this contract to the watchlist">Watch</th>
            <th scope="col" title="Copy this row as a markdown table for ChatGPT">Copy</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const strike = moneyStrike(row.strike_exact, row.strike_cents)
            const copied = copiedKey === copyRowStateKey(ticker, row.expiration, row.strike_exact ?? row.strike_cents)
            return (
              <tr key={row.watch_key ?? `${row.expiration}-${row.strike_exact ?? row.strike_cents}-${index}`}>
                {columns.map((column, index) => (
                  <Fragment key={column.id}>
                    <HeatCell
                      heading={index === 0}
                      value={column.accessor(row)}
                      range={ranges[column.id] ?? null}
                    >
                      {column.format(row)}
                    </HeatCell>
                    {index === 0 ? <td className="odds-cell"><OddsValues odds={row.market_odds} compact /></td> : null}
                  </Fragment>
                ))}
                <td className="watch-cell">
                  <WatchButton
                    contractLabel={`${ticker} ${row.expiration} ${strike} strike`}
                    watchKey={row.watch_key}
                    watchabilityReason={row.watchability_reason}
                    state={row.watch_key ? watchStates[row.watch_key] : undefined}
                    onWatch={onWatch}
                  />
                </td>
                <td className="copy-cell">
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    className="copy-row-button"
                    data-copied={copied ? "true" : undefined}
                    aria-label={copyRowAccessibleName(ticker, row.expiration, strike)}
                    onClick={() => onCopy(row, group)}
                  >
                    {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
                  </Button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function MobileResults({
  ticker,
  side,
  columns,
  rows,
  group,
  ranges,
  copiedKey,
  sort,
  onSort,
  onCopy,
  watchStates,
  onWatch,
}: Omit<Parameters<typeof DesktopResults>[0], "density"> & { side: Side }) {
  const priority = mobilePriorityColumns(columns, side)
  const remaining = columns.filter((column) => !priority.some((item) => item.id === column.id))
  const activeSort = columns.find((column) => column.id === sort.id) ?? columns[0]
  return (
    <div className="mobile-results">
      <div className="mobile-sort-row">
        <label>
          <span>Sort by</span>
          <select value={activeSort.id} onChange={(event) => onSort(event.target.value)}>
            {columns.map((column) => <option key={column.id} value={column.id}>{column.label}</option>)}
          </select>
        </label>
        <Button type="button" variant="outline" aria-label={`Sort ${sort.dir === "asc" ? "descending" : "ascending"}`} onClick={() => onSort(activeSort.id)}>
          {sort.dir === "asc" ? <ArrowUp aria-hidden="true" /> : <ArrowDown aria-hidden="true" />}
          {sort.dir === "asc" ? "Ascending" : "Descending"}
        </Button>
      </div>
      <div className="mobile-row-list">
        {rows.map((row, index) => {
          const copied = copiedKey === copyRowStateKey(ticker, row.expiration, row.strike_exact ?? row.strike_cents)
          const strike = moneyStrike(row.strike_exact, row.strike_cents)
          const rowName = `${ticker} ${row.expiration} strike ${strike}`
          const summary = priority.map((column) => `${column.label} ${column.format(row)}`).join(", ")
          const odds = row.market_odds
          return (
            <Collapsible key={row.watch_key ?? `${row.expiration}-${row.strike_exact ?? row.strike_cents}-${index}`} className="mobile-option-row">
              <CollapsibleTrigger className="mobile-row-summary" aria-label={`Show details for ${rowName}. ${summary}. Market odds: ${oddsLabel(odds)}`}>
                <span className="mobile-row-main">
                  <span className="mobile-priority-grid">
                    {priority.map((column) => (
                      <span key={column.id} {...heatProps(column.accessor(row), ranges[column.id] ?? null)}>
                        <small>{column.label}</small>
                        <strong className="font-mono">{column.format(row)}</strong>
                      </span>
                    ))}
                  </span>
                  <span className="mobile-row-odds"><OddsValues odds={odds} compact /></span>
                </span>
                <ChevronRight className="row-chevron" aria-hidden="true" />
              </CollapsibleTrigger>
              <CollapsibleContent className="mobile-row-details">
                {remaining.length > 0 ? (
                  <dl>
                    {remaining.map((column) => (
                      <div key={column.id}>
                        <dt>{column.label}</dt>
                        <dd className="font-mono">{column.format(row)}</dd>
                      </div>
                    ))}
                  </dl>
                ) : null}
                <WatchButton
                  contractLabel={rowName}
                  watchKey={row.watch_key}
                  watchabilityReason={row.watchability_reason}
                  state={row.watch_key ? watchStates[row.watch_key] : undefined}
                  mobile
                  onWatch={onWatch}
                />
                <Button
                  type="button"
                  variant="outline"
                  className="mobile-copy-button"
                  data-copied={copied ? "true" : undefined}
                  aria-label={copyRowAccessibleName(ticker, row.expiration, strike)}
                  onClick={() => onCopy(row, group)}
                >
                  {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
                  {copied ? "Copied" : "Copy selected metrics"}
                </Button>
              </CollapsibleContent>
            </Collapsible>
          )
        })}
      </div>
    </div>
  )
}

export default function ExpiryTables({
  ticker,
  side,
  columns,
  groups,
  mountedGroups,
  expandedExpirations,
  copiedKey,
  sort,
  density,
  onToggleExpiration,
  onSort,
  onCopy,
  watchStates,
  onWatch,
}: Props) {
  const mobile = useMediaQuery("(max-width: 39.999rem)")
  const mounted = new Map(mountedGroups.map((item) => [item.group.expiration, item]))
  return (
    <div className="expiry-list">
      {groups.map((item) => {
        const { group, ranges } = item
        const expanded = expandedExpirations.has(group.expiration)
        const rows = mounted.get(group.expiration)?.visible ?? []
        const headingId = `expiry-${group.expiration}`
        return (
          <section key={group.expiration} className="expiry-block">
          <Collapsible open={expanded} onOpenChange={() => onToggleExpiration(group.expiration)}>
            <div className="expiry-heading">
              <h2 id={headingId}>
                <CollapsibleTrigger className="expiry-trigger">
                  <ChevronRight className="expiry-chevron" aria-hidden="true" />
                  <span>{group.expiration}</span>
                  <span className="expiry-dte">{integer(group.dte)} DTE</span>
                </CollapsibleTrigger>
              </h2>
              <Badge
                variant="secondary"
                aria-label={`${item.visible.length} ${item.visible.length === 1 ? "contract" : "contracts"}`}
              >
                {item.visible.length}
                <span className="expiry-count-label">{item.visible.length === 1 ? " contract" : " contracts"}</span>
              </Badge>
            </div>
            <CollapsibleContent className="expiry-content" aria-labelledby={headingId}>
              {rows.length === 0 ? (
                <p className="control-note expiry-reveal-note">Use the reveal controls to mount rows in this expiration.</p>
              ) : mobile ? (
                <MobileResults
                  ticker={ticker}
                  side={side}
                  columns={columns}
                  rows={rows}
                  group={group}
                  ranges={ranges}
                  copiedKey={copiedKey}
                  sort={sort}
                  onSort={onSort}
                  onCopy={onCopy}
                  watchStates={watchStates}
                  onWatch={onWatch}
                />
              ) : (
                <DesktopResults
                  ticker={ticker}
                  columns={columns}
                  rows={rows}
                  group={group}
                  ranges={ranges}
                  copiedKey={copiedKey}
                  sort={sort}
                  density={density}
                  onSort={onSort}
                  onCopy={onCopy}
                  watchStates={watchStates}
                  onWatch={onWatch}
                />
              )}
            </CollapsibleContent>
          </Collapsible>
          </section>
        )
      })}
    </div>
  )
}
