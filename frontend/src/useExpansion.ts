import { useMemo, useState } from "react"

import { parseThreshold, type FilterTexts } from "./filters"
import type { ChainPage, Side } from "./types"
import { nearestMatchingExpiration } from "./viewModel"

type Seed = FilterTexts & { contracts: number }

export function useExpansion(
  identityKey: string,
  page: ChainPage | null,
  side: Side,
  texts: FilterTexts,
  contracts: number,
) {
  const [state, setState] = useState<{ identity: string; values: Set<string> | null; seed: Seed }>(() => ({
    identity: identityKey,
    values: null,
    seed: { primary: "", apr: "", drop: "", minDte: "", maxDte: "", contracts: 1 },
  }))
  const seed = useMemo(
    () => (state.identity === identityKey ? state.seed : { ...texts, contracts }),
    [state, identityKey, texts, contracts],
  )
  const seededExpiration = useMemo(() => nearestMatchingExpiration(page, seed.contracts, {
    primary: parseThreshold(seed.primary),
    apr: parseThreshold(seed.apr),
    drop: parseThreshold(seed.drop),
    minDte: parseThreshold(seed.minDte),
    maxDte: parseThreshold(seed.maxDte),
  }, side), [page, side, seed])
  const stored = state.identity === identityKey ? state.values : null
  const expanded = useMemo(
    () => stored ?? new Set(seededExpiration ? [seededExpiration] : []),
    [seededExpiration, stored],
  )
  const currentSeed = (): Seed => ({ ...texts, contracts })
  const reset = (nextIdentity: string) => {
    setState({ identity: nextIdentity, values: null, seed: currentSeed() })
  }
  const update = (next: Set<string> | ((current: Set<string>) => Set<string>)) => {
    const values = typeof next === "function" ? next(expanded) : next
    setState({
      identity: identityKey,
      values,
      seed: state.identity === identityKey ? state.seed : currentSeed(),
    })
  }
  return { expanded, reset, update }
}
