import { useState } from "react"

import { EMPTY_FILTER_TEXTS, parseThreshold, type FilterId, type FilterState, type FilterTexts } from "./filters"

export function useChainFilters() {
  const [texts, setTexts] = useState<FilterTexts>(EMPTY_FILTER_TEXTS)
  const setText = (id: FilterId, value: string) => {
    setTexts((current) => ({ ...current, [id]: value }))
  }
  const clear = () => setTexts(EMPTY_FILTER_TEXTS)
  const parsed: FilterState = {
    primary: parseThreshold(texts.primary),
    apr: parseThreshold(texts.apr),
    drop: parseThreshold(texts.drop),
    minDte: parseThreshold(texts.minDte),
    maxDte: parseThreshold(texts.maxDte),
  }
  const key = `${texts.primary}|${texts.apr}|${texts.drop}|${texts.minDte}|${texts.maxDte}`
  return { texts, parsed, setText, clear, key }
}
