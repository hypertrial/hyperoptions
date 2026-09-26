import { lazy, Suspense, useState } from "react"
import { BrowserRouter, Link, MemoryRouter, Navigate, Route, Routes, useLocation } from "react-router-dom"

import ItmChain from "./ItmChain"
import "./index.css"

const Watchlist = lazy(() => import("./watchlist/Watchlist"))

function Workspace() {
  const location = useLocation()
  const [lastChainUrl, setLastChainUrl] = useState("/")
  const watchlist = location.pathname === "/watchlist"
  const liveChainUrl = location.pathname === "/" ? `${location.pathname}${location.search}` : null
  if (liveChainUrl !== null && liveChainUrl !== lastChainUrl) setLastChainUrl(liveChainUrl)
  const chainUrl = liveChainUrl ?? lastChainUrl

  return (
    <div className="app">
      <a className="skip-link" href="#main-content" aria-controls="main-content">Skip to main content</a>
      <nav className="workspace-nav" aria-label="Workstation">
        <Link aria-current={location.pathname === "/" ? "page" : undefined} to={chainUrl}>Option chain</Link>
        <Link aria-current={watchlist ? "page" : undefined} to="/watchlist">Watchlist</Link>
      </nav>
      <Routes>
        <Route path="/" element={<ItmChain />} />
        <Route path="/research/*" element={<Navigate to="/watchlist" replace />} />
        <Route path="/watchlist" element={<Suspense fallback={<p>Loading watchlist…</p>}><Watchlist chainUrl={lastChainUrl} /></Suspense>} />
        <Route path="*" element={<main id="main-content" className="empty-state route-not-found"><h1>Page not found</h1><p>This address does not match a workstation page.</p><p><Link to={chainUrl}>Open the option chain</Link> or <Link to="/watchlist">go to the watchlist</Link>.</p></main>} />
      </Routes>
    </div>
  )
}

export default function App() {
  const Router = typeof window === "undefined" ? MemoryRouter : BrowserRouter
  return <Router><Workspace /></Router>
}
