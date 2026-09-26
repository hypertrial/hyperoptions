import type { StrategyDetail } from "../api/types"

const SEGMENT_FILL: Record<string, string> = {
  train: "rgba(111,101,91,0.12)",
  validation: "rgba(154,52,18,0.10)",
  test: "rgba(31,122,77,0.12)",
}

export function segmentSwatch(dark: boolean): Record<"train" | "validation" | "test", string> {
  return dark
    ? { train: "#94a3b8", validation: "#fbbf24", test: "#4ade80" }
    : { train: "#475569", validation: "#a16207", test: "#15803d" }
}

export function performanceFigure(detail: StrategyDetail, dark = false): { data: object[]; layout: object } {
  const colors = dark
    ? { close: "#f1f5f9", entry: "#4ade80", exit: "#fb7185", strategy: "#fbbf24", hold: "#94a3b8" }
    : { close: "#1c1612", entry: "#15803d", exit: "#be123c", strategy: "#a16207", hold: "#475569" }
  const shapes = ["train", "validation", "test"].flatMap((name) => {
    const bound = detail.segment_bounds?.[name]
    if (!bound?.start || !bound.end) return []
    return [
      {
        type: "rect",
        xref: "x",
        yref: "paper",
        x0: bound.start,
        x1: bound.end,
        y0: 0,
        y1: 1,
        fillcolor: SEGMENT_FILL[name],
        line: { width: 0 },
        layer: "below",
      },
    ]
  })
  return {
    data: [
      { x: detail.dates, y: detail.close, type: "scatter", mode: "lines", name: "Close", xaxis: "x", yaxis: "y", line: { color: colors.close } },
      { x: detail.dates, y: detail.entry_marks, type: "scatter", mode: "markers", name: "Entry", xaxis: "x", yaxis: "y", marker: { color: colors.entry, size: 9, symbol: "triangle-up" } },
      { x: detail.dates, y: detail.exit_marks, type: "scatter", mode: "markers", name: "Exit", xaxis: "x", yaxis: "y", marker: { color: colors.exit, size: 9, symbol: "triangle-down" } },
      { x: detail.dates, y: detail.equity, type: "scatter", mode: "lines", name: "Strategy", xaxis: "x2", yaxis: "y2", line: { color: colors.strategy } },
      { x: detail.dates, y: detail.buy_hold, type: "scatter", mode: "lines", name: "Buy & hold", xaxis: "x2", yaxis: "y2", line: { color: colors.hold } },
      { x: detail.dates, y: detail.drawdown, type: "scatter", mode: "lines", name: "Drawdown", xaxis: "x3", yaxis: "y3", fill: "tozeroy", line: { color: colors.exit } },
    ],
    layout: {
      autosize: true,
      hovermode: "x unified",
      showlegend: true,
      legend: { orientation: "h", font: { size: 11 } },
      shapes,
      margin: { t: 16, r: 12, b: 72, l: 64 },
      xaxis: { anchor: "y", domain: [0, 1], showticklabels: false },
      yaxis: { domain: [0.7, 1], title: { text: "Price" } },
      xaxis2: { anchor: "y2", matches: "x", showticklabels: false },
      yaxis2: { domain: [0.36, 0.64], title: { text: "Equity" } },
      xaxis3: { anchor: "y3", matches: "x" },
      yaxis3: { domain: [0, 0.28], title: { text: "Drawdown" } },
    },
  }
}
