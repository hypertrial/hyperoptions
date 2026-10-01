import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { fetchTickers } from "./api"
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
  const [retry, setRetry] = useState(0)
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
    const controller = new AbortController()
    const handle = window.setTimeout(() => {
      setLoading(true)
      void fetchTickers(query, 10, controller.signal).then((page) => {
        if (id !== requestId.current) return
        setUnavailable(false)
        setResults(page.results)
        setActive(0)
        setLoading(false)
      }).catch(() => {
        if (id !== requestId.current) return
        setUnavailable(true)
        setResults([])
        setLoading(false)
      })
    }, DEBOUNCE_MS)
    return () => {
      window.clearTimeout(handle)
      if (id === requestId.current) requestId.current += 1
      controller.abort()
    }
  }, [disabled, query, retry, ticker])

  useEffect(() => {
    if (open && results[active]) {
      document.getElementById(`${listId}-${results[active].symbol}`)?.scrollIntoView?.({ block: "nearest" })
    }
  }, [open, active, results, listId])

  const choose = (symbol: string) => {
    onSelect(symbol)
    setQuery(symbol)
    setOpen(false)
  }

  const changeQuery = (value: string) => {
    const next = value.toUpperCase()
    setOpen(true)
    if (next === query) return
    requestId.current += 1
    setQuery(next)
    setResults([])
    setActive(0)
    setLoading(true)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault()
      setOpen(true)
      setActive((index) => open ? Math.min(index + 1, Math.max(results.length - 1, 0)) : 0)
      return
    }
    if (event.key === "ArrowUp") {
      event.preventDefault()
      setOpen(true)
      setActive((index) => open ? Math.max(index - 1, 0) : Math.max(results.length - 1, 0))
      return
    }
    if (event.key === "Enter" && open) {
      event.preventDefault()
      const selected = results[active]
      if (selected && !unavailable) choose(selected.symbol)
      return
    }
    if (event.key === "Escape") {
      if (open) event.stopPropagation()
      setOpen(false)
    }
  }

  const blocked = Boolean(disabled || unavailable)

  return (
    <div className="ticker-picker">
      <label className="control-field w-full">
        <span>Ticker</span>
        <Input
          id="ticker-picker"
          role="combobox"
          value={query}
          autoComplete="off"
          spellCheck={false}
          disabled={blocked}
          aria-autocomplete="list"
          aria-expanded={open && !blocked}
          aria-controls={open && !blocked ? listId : undefined}
          aria-activedescendant={open && !blocked && results[active] ? `${listId}-${results[active].symbol}` : undefined}
          onFocus={() => setOpen(true)}
          onClick={() => setOpen(true)}
          onBlur={() => setOpen(false)}
          onKeyDown={onKeyDown}
          onChange={(event) => changeQuery(event.currentTarget.value)}
          className="w-full"
        />
      </label>
      {unavailable ? (
        <div>
          <p className="control-note" role="status">Ticker list unavailable</p>
          <Button type="button" variant="link" size="xs" disabled={loading} onClick={() => {
            setLoading(true)
            setRetry((value) => value + 1)
          }}>Retry ticker list</Button>
        </div>
      ) : null}
      {open && !blocked ? (
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
              onPointerDown={(event) => event.preventDefault()}
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
