import { describe, expect, it } from "vitest"

import { columnGroup } from "./ColumnPicker"
import { strategyColumns } from "./columns"
import type { Side } from "./types"

function columnSnapshot(side: Side) {
  return strategyColumns(side).map((column) => ({
    id: column.id,
    label: column.label,
    info: column.info,
    abbrev: column.abbrev ?? false,
    heatmap: column.heatmap,
    greek: column.greek ?? false,
    group: columnGroup(column),
  }))
}

describe("strategy column metadata", () => {
  it.each(["call", "put"] as const)("matches the %s snapshot", (side) => {
    expect(columnSnapshot(side)).toMatchSnapshot()
  })
})
