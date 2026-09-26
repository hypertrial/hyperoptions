import { useEffect, useRef } from "react"

type PlotlyChartProps = {
  data: object[]
  layout?: object
  className?: string
}

export function PlotlyChart({ data, layout, className }: PlotlyChartProps) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const node = ref.current
    if (!node) return
    let active = true
    let purge: (() => void) | undefined
    void import("plotly.js-dist-min").then(({ default: Plotly }) => {
      if (!active) return
      const style = getComputedStyle(node)
      Plotly.react(
        node,
        data,
        {
          margin: { t: 24, r: 16, b: 36, l: 56 },
          paper_bgcolor: "transparent",
          plot_bgcolor: "transparent",
          font: { color: style.color, family: style.fontFamily },
          xaxis: { gridcolor: "rgba(128, 128, 128, .2)" },
          yaxis: { gridcolor: "rgba(128, 128, 128, .2)" },
          legend: { orientation: "h" },
          ...layout,
        },
        { responsive: true, displayModeBar: false },
      )
      purge = () => Plotly.purge(node)
    })
    return () => {
      active = false
      purge?.()
    }
  }, [data, layout])
  return <div ref={ref} className={className ?? "h-72 w-full"} />
}
