import { useState } from "react"

import { EMPTY_FILTER_TEXTS, parseThreshold, type FilterId, type FilterState, type FilterTexts } from "./filters"

export function useChainFilters() {
  const [texts, setTexts] = useState<FilterTexts>(EMPTY_FILTER_TEXTS)
  const setText = (id: FilterId, value: string) => {
    setTexts((current) => ({ ...current, [id]: value }))
  }
  const clear = () => setTexts(EMPTY_FILTER_TEXTS)
  const parsed: FilterState = {
    premium: parseThreshold(texts.premium),
    apr: parseThreshold(texts.apr),
    breakeven: parseThreshold(texts.breakeven),
    minIv: parseThreshold(texts.minIv),
    minDte: parseThreshold(texts.minDte),
    maxDte: parseThreshold(texts.maxDte),
  }
  const key = `${texts.premium}|${texts.apr}|${texts.breakeven}|${texts.minIv}|${texts.minDte}|${texts.maxDte}`
  return { texts, parsed, setText, clear, key }
}
