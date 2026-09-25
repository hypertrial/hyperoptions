import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react"

import {
  Combobox,
  ComboboxInput,
} from "@/components/ui/combobox"
import { ApiError, fetchTickers } from "./api"
import type { TickerListing } from "./types"

const DEBOUNCE_MS = 150

type Props = {
  ticker: string
  disabled?: boolean
  onSelect: (symbol: string) => void
}

export default function TickerPicker({ ticker, disabled, onSelect }: Props) {
  const listId = useId()
  const [query, setQuery] = useState(ticker)
  const [open, setOpen] = useState(false)
  const [results, setResults] = useState<TickerListing[]>([])
  const [active, setActive] = useState(0)
  const [unavailable, setUnavailable] = useState(false)
  const [loading, setLoading] = useState(true)
  const requestId = useRef(0)

  useEffect(() => {
    requestId.current += 1
    setQuery(ticker)
    setResults([])
    setActive(0)
    setLoading(true)
  }, [ticker])

  useEffect(() => {
    if (disabled) return
    const id = ++requestId.current
    const handle = window.setTimeout(() => {
      setLoading(true)
      void fetchTickers(query).then((page) => {
        if (id !== requestId.current) return
        setUnavailable(false)
        setResults(page.results)
        setActive(0)
        setLoading(false)
      }).catch((error: unknown) => {
        if (id !== requestId.current) return
        setUnavailable(error instanceof ApiError && error.status === 503)
        setResults([])
        setLoading(false)
      })
    }, DEBOUNCE_MS)
    return () => window.clearTimeout(handle)
  }, [disabled, query])

  const choose = (symbol: string) => {
    onSelect(symbol)
    setQuery(symbol)
    setOpen(false)
  }

  const changeQuery = (value: string) => {
    requestId.current += 1
    setQuery(value.toUpperCase())
    setResults([])
    setActive(0)
    setLoading(true)
    setOpen(true)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault()
      setOpen(true)
      setActive((index) => Math.min(index + 1, Math.max(results.length - 1, 0)))
      return
    }
    if (event.key === "ArrowUp") {
      event.preventDefault()
      setActive((index) => Math.max(index - 1, 0))
      return
    }
    if (event.key === "Home") {
      event.preventDefault()
      setActive(0)
      return
    }
    if (event.key === "End") {
      event.preventDefault()
      setActive(Math.max(results.length - 1, 0))
      return
    }
    if (event.key === "Enter") {
      event.preventDefault()
      const selected = results[active]
      if (selected && !unavailable) choose(selected.symbol)
      return
    }
    if (event.key === "Escape") {
      setOpen(false)
    }
  }

  const blocked = Boolean(disabled || unavailable)

  return (
    <div className="ticker-picker">
      <label className="control-field w-full">
        <span>Ticker</span>
        <Combobox
          items={results}
          value={results.find((item) => item.symbol === ticker) ?? null}
          inputValue={query}
          onInputValueChange={changeQuery}
          filter={null}
          itemToStringValue={(item) => item.symbol}
          disabled={blocked}
        >
          <ComboboxInput
            id="ticker-picker"
            autoComplete="off"
            spellCheck={false}
            disabled={blocked}
            aria-controls={listId}
            aria-activedescendant={open && results[active] ? `${listId}-${results[active].symbol}` : undefined}
            onFocus={() => setOpen(true)}
            onBlur={(event) => {
              const next = event.relatedTarget as Node | null
              if (next && event.currentTarget.closest(".ticker-picker")?.contains(next)) return
              window.setTimeout(() => setOpen(false), 150)
            }}
            onKeyDown={onKeyDown}
            onChange={(event) => changeQuery(event.currentTarget.value)}
            className="w-full"
          />
        </Combobox>
      </label>
      {unavailable ? <p className="control-note" role="status">Ticker list unavailable</p> : null}
      {open && !unavailable ? (
        <ul
          id={listId}
          role="listbox"
          className="ticker-list"
          aria-label="Matching tickers"
          aria-busy={loading}
          aria-live="polite"
        >
          {results.map((item, index) => (
            <li
              key={item.symbol}
              id={`${listId}-${item.symbol}`}
              role="option"
              aria-selected={index === active}
              className={index === active ? "active" : undefined}
              onClick={() => choose(item.symbol)}
            >
              <strong>{item.symbol}</strong>
              <span>{item.name}</span>
            </li>
          ))}
          {results.length === 0 ? (
            <li className="empty" role="option" aria-disabled="true">
              {loading ? "Searching tickers…" : "No matches"}
            </li>
          ) : null}
        </ul>
      ) : null}
    </div>
  )
}
