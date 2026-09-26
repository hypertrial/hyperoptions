import { lazy, Suspense, useState } from "react"
import { BrowserRouter, Link, MemoryRouter, Navigate, Route, Routes, useLocation } from "react-router-dom"

import ItmChain from "./ItmChain"
import "./index.css"

const Watchlist = lazy(() => import("./watchlist/Watchlist"))

function Workspace() {
  const location = useLocation()
  const [lastChainUrl, setLastChainUrl] = useState("/")
  const watchlist = location.pathname === "/watchlist"

  const rememberChainUrl = () => {
    if (location.pathname === "/") setLastChainUrl(`${location.pathname}${location.search}`)
  }

  return (
    <div className="app">
      <a className="skip-link" href="#main-content" aria-controls="main-content">
        Skip to {watchlist ? "watchlist" : "chain"}
      </a>
      <nav className="workspace-nav" aria-label="Workstation">
        <Link aria-current={location.pathname === "/" ? "page" : undefined} to={lastChainUrl}>Option chain</Link>
        <Link aria-current={watchlist ? "page" : undefined} to="/watchlist" onClick={rememberChainUrl}>Watchlist</Link>
      </nav>
      <Routes>
        <Route path="/" element={<ItmChain />} />
        <Route path="/research/*" element={<Navigate to="/watchlist" replace />} />
        <Route path="/watchlist" element={<Suspense fallback={<p>Loading watchlist…</p>}><Watchlist chainUrl={lastChainUrl} /></Suspense>} />
      </Routes>
    </div>
  )
}

export default function App() {
  const Router = typeof window === "undefined" ? MemoryRouter : BrowserRouter
  return <Router><Workspace /></Router>
}
