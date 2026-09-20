import { Drawer } from "@base-ui/react/drawer"
import { RefreshCw, Settings2, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { dateTime, moneyCents } from "./format"
import { MoneynessToggle, StrategyToggle } from "./Segmented"
import ThemeToggle from "./ThemeToggle"
import TickerPicker from "./TickerPicker"
import type { ChainPage, Moneyness, Side } from "./types"
import { useMediaQuery } from "./useMediaQuery"

export type SessionInfo = {
  label: string
  state: "open" | "closed" | "unknown"
  detail: string | null
}

type Props = {
  ticker: string
  side: Side
  moneyness: Moneyness
  page: ChainPage | null
  session: SessionInfo
  currentSource: string
  quoteStamp?: string
  contractsText: string
  contractsInvalid: boolean
  contractsHelp: string
  loading: boolean
  onSelectTicker: (ticker: string) => void
  onSelectSide: (side: Side) => void
  onSelectMoneyness: (moneyness: Moneyness) => void
  onContractsChange: (value: string) => void
  onRefresh: () => void
}

function MarketStatus({ session }: { session: SessionInfo }) {
  return (
    <p className="market-session-status" data-state={session.state}>
      <span className="market-session-dot" aria-hidden="true" />
      <strong>{session.label}</strong>
      {session.detail ? <span>{session.detail}</span> : null}
    </p>
  )
}

function MarketControls({
  ticker,
  side,
  moneyness,
  page,
  session,
  currentSource,
  quoteStamp,
  contractsText,
  contractsInvalid,
  contractsHelp,
  loading,
  onSelectTicker,
  onSelectSide,
  onSelectMoneyness,
  onContractsChange,
  onRefresh,
}: Props) {
  const quoteLoading = loading && page == null
  return (
    <div className="market-controls">
      <div className="market-brand-row">
        <div>
          <p className="eyebrow">Options workstation</p>
          <p className="market-brand">HyperOptions</p>
        </div>
        <ThemeToggle />
      </div>

      <TickerPicker ticker={ticker} onSelect={onSelectTicker} />

      <section className="quote-card" aria-label={`${ticker} market summary`} aria-busy={quoteLoading}>
        <div className="quote-identity">
          <span>{ticker}</span>
          {page?.name ? <p title={page.name}>{page.name}</p> : null}
        </div>
        {quoteLoading ? (
          <div className="quote-loading">
            <RefreshCw className="animate-spin" aria-hidden="true" />
            <div>
              <strong>Loading {ticker} quote</strong>
              <span>Fetching price and market session…</span>
            </div>
          </div>
        ) : (
          <>
            <p className="quote-price font-mono">{moneyCents(page?.current_cents)}</p>
            <MarketStatus session={session} />
            <dl className="quote-details">
              <div><dt>Price source</dt><dd>{currentSource}</dd></div>
              <div><dt>Quote time</dt><dd>{quoteStamp || "Unavailable"}</dd></div>
              <div><dt>Fetched</dt><dd>{page ? dateTime(page.fetched_at) : "Unavailable"}</dd></div>
            </dl>
          </>
        )}
      </section>

      <div className="market-control-stack">
        <StrategyToggle value={side} onChange={onSelectSide} />
        <MoneynessToggle value={moneyness} onChange={onSelectMoneyness} />
      </div>

      <div className="contract-control">
        <Label className="control-field items-stretch" htmlFor="contracts">
          <span>Contracts</span>
          <Input
            id="contracts"
            type="text"
            inputMode="numeric"
            autoComplete="off"
            placeholder="1"
            value={contractsText}
            aria-invalid={contractsInvalid}
            aria-describedby="contracts-help"
            className="border-0 bg-transparent shadow-none focus-visible:border-0 focus-visible:ring-0"
            onChange={(event) => onContractsChange(event.target.value)}
          />
        </Label>
        <p id="contracts-help" className={contractsInvalid ? "field-error" : "contract-help"}>
          {contractsHelp}
        </p>
      </div>

      <Button type="button" variant="outline" className="refresh-button" onClick={onRefresh} disabled={loading}>
        <RefreshCw className={loading ? "animate-spin" : undefined} aria-hidden="true" />
        {loading && page ? "Refreshing" : "Refresh data"}
      </Button>
    </div>
  )
}

export default function CommandBar(props: Props) {
  const desktop = useMediaQuery("(min-width: 64rem)", true)
  const quoteLoading = props.loading && props.page == null
  const compactSession: SessionInfo = {
    ...props.session,
    label: props.session.state === "open" ? "Open" : props.session.state === "closed" ? "Closed" : "Unavailable",
    detail: null,
  }

  if (desktop) {
    return (
      <aside className="market-sidebar" aria-label="Market analysis settings">
        <MarketControls {...props} />
      </aside>
    )
  }

  return (
    <Drawer.Root modal swipeDirection="left">
      <div className="mobile-market-bar">
        <div className="mobile-market-quote" aria-busy={quoteLoading}>
          <strong>{props.ticker}</strong>
          {quoteLoading ? (
            <span className="mobile-loading-label">
              <RefreshCw className="animate-spin" aria-hidden="true" />
              Loading market data…
            </span>
          ) : (
            <>
              <span className="font-mono">{moneyCents(props.page?.current_cents)}</span>
              <MarketStatus session={compactSession} />
            </>
          )}
        </div>
        <Drawer.Trigger className="secondary-button mobile-settings-trigger">
          <Settings2 aria-hidden="true" />
          Settings
        </Drawer.Trigger>
      </div>
      <Drawer.Portal>
        <Drawer.Backdrop className="drawer-backdrop" />
        <Drawer.Viewport className="drawer-viewport">
          <Drawer.Popup className="drawer-popup" initialFocus>
            <Drawer.Content className="drawer-content">
              <div className="drawer-heading">
                <div>
                  <Drawer.Title>Market setup</Drawer.Title>
                  <Drawer.Description>Change the ticker, strategy, sizing, and quote settings.</Drawer.Description>
                </div>
                <Drawer.Close className="drawer-close" aria-label="Close settings">
                  <X aria-hidden="true" />
                </Drawer.Close>
              </div>
              <MarketControls {...props} />
            </Drawer.Content>
          </Drawer.Popup>
        </Drawer.Viewport>
      </Drawer.Portal>
    </Drawer.Root>
  )
}
