import ItmChain from "./ItmChain"
import "./index.css"

export default function App() {
  return (
    <div className="app">
      <a className="skip-link" href="#main-content" aria-controls="main-content">Skip to chain</a>
      <ItmChain />
    </div>
  )
}
