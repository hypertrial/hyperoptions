import { lazy, Suspense, useState } from "react"
import { BrowserRouter, Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom"

import ItmChain from "./ItmChain"
import "./index.css"

const ResearchApp = lazy(() => import("./research/ResearchApp"))
const Overview = lazy(() => import("./research/pages/Overview").then((module) => ({ default: module.Overview })))
const Leaderboard = lazy(() => import("./research/pages/Leaderboard").then((module) => ({ default: module.Leaderboard })))
const StrategyDetail = lazy(() => import("./research/pages/StrategyDetail").then((module) => ({ default: module.StrategyDetail })))
const Watchlist = lazy(() => import("./watchlist/Watchlist"))

function Workspace() {
  const location = useLocation()
  const [lastChainUrl, setLastChainUrl] = useState("/")
  const research = location.pathname.startsWith("/research")
  const watchlist = location.pathname === "/watchlist"

  const rememberChainUrl = () => {
    if (location.pathname === "/") setLastChainUrl(`${location.pathname}${location.search}`)
  }

  return (
    <div className={`app${research ? " research" : ""}`}>
      <a className="skip-link" href="#main-content" aria-controls="main-content">
        Skip to {research ? "research" : watchlist ? "watchlist" : "chain"}
      </a>
      <nav className="workspace-nav" aria-label="Workstation">
        <Link aria-current={location.pathname === "/" ? "page" : undefined} to={lastChainUrl}>Option chain</Link>
        <Link
          aria-current={research ? "page" : undefined}
          to="/research"
          onClick={rememberChainUrl}
        >Research</Link>
        <Link aria-current={watchlist ? "page" : undefined} to="/watchlist" onClick={rememberChainUrl}>Watchlist</Link>
      </nav>
      <Routes>
        <Route path="/" element={<ItmChain />} />
        <Route path="/research" element={<Suspense fallback={<p>Loading research…</p>}><ResearchApp /></Suspense>}>
          <Route index element={<Suspense fallback={<p>Loading overview…</p>}><Overview /></Suspense>} />
          <Route path="leaderboard" element={<Suspense fallback={<p>Loading leaderboard…</p>}><Leaderboard /></Suspense>} />
          <Route path="strategies/:strategyId" element={<Suspense fallback={<p>Loading strategy…</p>}><StrategyDetail /></Suspense>} />
        </Route>
        <Route path="/watchlist" element={<Suspense fallback={<p>Loading watchlist…</p>}><Watchlist chainUrl={lastChainUrl} /></Suspense>} />
      </Routes>
    </div>
  )
}

export default function App() {
  const Router = typeof window === "undefined" ? MemoryRouter : BrowserRouter
  return <Router><Workspace /></Router>
}
