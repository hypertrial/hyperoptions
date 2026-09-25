import { useState } from "react"

import { INITIAL_REVEAL } from "./viewModel"

export function useRevealLimit(filterKey: string) {
  const [state, setState] = useState({ key: "", limit: INITIAL_REVEAL })
  const limit = state.key === filterKey ? state.limit : INITIAL_REVEAL
  const update = (next: number | ((current: number) => number)) => {
    const value = typeof next === "function" ? next(limit) : next
    setState({ key: filterKey, limit: value })
  }
  return { limit, update }
}
