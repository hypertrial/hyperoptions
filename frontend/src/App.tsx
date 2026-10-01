import { lazy, Suspense, useEffect, useState } from "react"
import { BrowserRouter, Link, MemoryRouter, Navigate, Route, Routes, useLocation } from "react-router-dom"

import ItmChain from "./ItmChain"
import { DEFAULT_FORECAST_MODEL, FORECAST_MODEL_KEY, PHYSICAL_MODEL_NAMES, PHYSICAL_MODELS, physicalModel, type PhysicalModel } from "./forecastModels"
import VersionBanner from "./VersionBanner"
import "./index.css"

const Watchlist = lazy(() => import("./watchlist/Watchlist"))

function Workspace() {
  const location = useLocation()
  const [lastChainUrl, setLastChainUrl] = useState("/")
  const [forecastModel, setForecastModel] = useState<PhysicalModel>(() => {
    try { return physicalModel(localStorage.getItem(FORECAST_MODEL_KEY)) ?? DEFAULT_FORECAST_MODEL }
    catch { return DEFAULT_FORECAST_MODEL }
  })
  useEffect(() => {
    try { localStorage.setItem(FORECAST_MODEL_KEY, forecastModel) } catch { /* Selection still works for this session. */ }
  }, [forecastModel])
  const watchlist = location.pathname === "/watchlist"
  useEffect(() => {
    const page = location.pathname === "/" ? "Option chain" : watchlist ? "Watchlist" : "Page not found"
    document.title = `${page} · HyperOptions`
  }, [location.pathname, watchlist])
  const liveChainUrl = location.pathname === "/" ? `${location.pathname}${location.search}` : null
  if (liveChainUrl !== null && liveChainUrl !== lastChainUrl) setLastChainUrl(liveChainUrl)
  const chainUrl = liveChainUrl ?? lastChainUrl

  return (
    <div className="app">
      <a className="skip-link" href="#main-content" aria-controls="main-content">Skip to main content</a>
      <div className="workspace-bar">
        <nav className="workspace-nav" aria-label="Workstation">
          <Link aria-current={location.pathname === "/" ? "page" : undefined} to={chainUrl}>Option chain</Link>
          <Link aria-current={watchlist ? "page" : undefined} to="/watchlist">Watchlist</Link>
        </nav>
        <label className="model-selector">Stock forecast model
          <select value={forecastModel} onChange={(event) => {
            const next = physicalModel(event.target.value)
            if (next) setForecastModel(next)
          }}>
            {PHYSICAL_MODELS.map((method) => <option key={method} value={method}>{PHYSICAL_MODEL_NAMES[method]}{method === DEFAULT_FORECAST_MODEL ? " · baseline" : " · experimental"}</option>)}
          </select>
        </label>
        <VersionBanner />
      </div>
      <Routes>
        <Route path="/" element={<ItmChain forecastModel={forecastModel} />} />
        <Route path="/research/*" element={<Navigate to="/watchlist" replace />} />
        <Route path="/watchlist" element={<Suspense fallback={<main id="main-content" className="watchlist-page" tabIndex={-1} aria-busy="true"><p role="status">Loading watchlist…</p></main>}><Watchlist chainUrl={lastChainUrl} forecastModel={forecastModel} /></Suspense>} />
        <Route path="*" element={<main id="main-content" className="empty-state route-not-found"><h1>Page not found</h1><p>This address does not match a workstation page.</p><p><Link to={chainUrl}>Open the option chain</Link> or <Link to="/watchlist">go to the watchlist</Link>.</p></main>} />
      </Routes>
    </div>
  )
}

export default function App() {
  const Router = typeof window === "undefined" ? MemoryRouter : BrowserRouter
  return <Router><Workspace /></Router>
}
