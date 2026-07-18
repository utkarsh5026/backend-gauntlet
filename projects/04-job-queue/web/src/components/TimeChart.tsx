import { useEffect, useMemo, useRef, useState } from 'react'

import { cn } from '@/lib/utils'

export interface ChartSeries {
  key: string
  label: string
  /** A CSS color — a `var(--chart-*)` token. */
  color: string
  values: number[]
}

interface TimeChartProps {
  series: ChartSeries[]
  /** Epoch-ms per sample, aligned to every series' `values`. */
  times: number[]
  height?: number
  /** Stack the series as areas (totals) vs. overlay them as lines. */
  stacked?: boolean
  /** Format a y value for the axis + legend + tooltip. */
  valueFormat?: (n: number) => string
  /** Short unit shown after tooltip values, e.g. "jobs" or "/s". */
  unit?: string
  className?: string
}

const PAD = { l: 40, r: 14, t: 12, b: 20 }

/** Round a max up to a clean axis ceiling (1·10ⁿ, 2·10ⁿ, 5·10ⁿ). */
function niceCeil(v: number): number {
  if (v <= 0) return 1
  const pow = Math.pow(10, Math.floor(Math.log10(v)))
  const n = v / pow
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10
  return step * pow
}

function useWidth() {
  const ref = useRef<HTMLDivElement>(null)
  const [w, setW] = useState(640)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) setW(e.contentRect.width)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, w] as const
}

export function TimeChart({
  series,
  times,
  height = 200,
  stacked = false,
  valueFormat = (n) => `${Math.round(n)}`,
  unit,
  className,
}: TimeChartProps) {
  const [ref, width] = useWidth()
  const [hover, setHover] = useState<number | null>(null)

  const n = times.length
  const x0 = PAD.l
  const x1 = Math.max(x0 + 1, width - PAD.r)
  const y0 = PAD.t
  const y1 = height - PAD.b

  const xAt = (i: number) => (n <= 1 ? x1 : x0 + (i / (n - 1)) * (x1 - x0))

  const yMax = useMemo(() => {
    let m = 0
    if (stacked) {
      for (let i = 0; i < n; i++) {
        let s = 0
        for (const ser of series) s += ser.values[i] ?? 0
        if (s > m) m = s
      }
    } else {
      for (const ser of series) for (const v of ser.values) if (v > m) m = v
    }
    return niceCeil(m)
  }, [series, n, stacked])

  const yAt = (v: number) => y1 - (v / yMax) * (y1 - y0)

  // Cumulative baselines for stacked areas, drawn low → high.
  const stacks = useMemo(() => {
    if (!stacked) return null
    const base = new Array(n).fill(0)
    return series.map((ser) => {
      const lower = base.slice()
      const upper = base.map((b, i) => b + (ser.values[i] ?? 0))
      for (let i = 0; i < n; i++) base[i] = upper[i]
      return { ser, lower, upper }
    })
  }, [series, n, stacked])

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * yMax)

  const hoverIdx = hover
  const tooltipLeft = hoverIdx !== null ? xAt(hoverIdx) : 0

  const hasData = n > 0
  const spanSec = n > 1 ? Math.round((times[n - 1] - times[0]) / 1000) : 0

  return (
    <div ref={ref} className={cn('relative w-full select-none', className)} style={{ height }}>
      {!hasData ? (
        <div className="text-muted-foreground absolute inset-0 flex items-center justify-center text-sm">
          waiting for data…
        </div>
      ) : (
        <svg
          width={width}
          height={height}
          className="overflow-visible"
          onPointerMove={(e) => {
            const rect = e.currentTarget.getBoundingClientRect()
            const px = e.clientX - rect.left
            const frac = (px - x0) / (x1 - x0)
            const i = Math.round(frac * (n - 1))
            setHover(Math.min(n - 1, Math.max(0, i)))
          }}
          onPointerLeave={() => setHover(null)}
        >
          {/* gridlines + y labels */}
          {ticks.map((t, i) => (
            <g key={i}>
              <line
                x1={x0}
                x2={x1}
                y1={yAt(t)}
                y2={yAt(t)}
                stroke="var(--chart-grid)"
                strokeWidth={1}
              />
              <text
                x={x0 - 8}
                y={yAt(t)}
                textAnchor="end"
                dominantBaseline="middle"
                className="fill-muted-foreground"
                fontSize={10}
              >
                {valueFormat(t)}
              </text>
            </g>
          ))}

          {/* stacked areas */}
          {stacks?.map(({ ser, lower, upper }) => {
            const top = upper.map((v, i) => `${xAt(i)},${yAt(v)}`)
            const bottom = lower.map((v, i) => `${xAt(i)},${yAt(v)}`).reverse()
            return (
              <g key={ser.key}>
                <polygon
                  points={[...top, ...bottom].join(' ')}
                  fill={ser.color}
                  fillOpacity={0.16}
                />
                {/* surface underlay + series line = a 2px gap between fills */}
                <polyline
                  points={top.join(' ')}
                  fill="none"
                  stroke="var(--card)"
                  strokeWidth={3.5}
                />
                <polyline
                  points={top.join(' ')}
                  fill="none"
                  stroke={ser.color}
                  strokeWidth={2}
                  strokeLinejoin="round"
                />
              </g>
            )
          })}

          {/* overlaid lines */}
          {!stacked &&
            series.map((ser) => {
              const pts = ser.values.map((v, i) => `${xAt(i)},${yAt(v)}`).join(' ')
              const lastX = xAt(n - 1)
              const lastY = yAt(ser.values[n - 1] ?? 0)
              return (
                <g key={ser.key}>
                  <polyline
                    points={pts}
                    fill="none"
                    stroke={ser.color}
                    strokeWidth={2}
                    strokeLinejoin="round"
                    strokeLinecap="round"
                  />
                  <circle cx={lastX} cy={lastY} r={3} fill={ser.color} />
                </g>
              )
            })}

          {/* baseline */}
          <line x1={x0} x2={x1} y1={y1} y2={y1} stroke="var(--chart-grid)" strokeWidth={1} />

          {/* x span caption */}
          {spanSec > 0 && (
            <text
              x={x1}
              y={height - 4}
              textAnchor="end"
              className="fill-muted-foreground"
              fontSize={10}
            >
              last {spanSec}s → now
            </text>
          )}

          {/* crosshair */}
          {hoverIdx !== null && (
            <g pointerEvents="none">
              <line
                x1={xAt(hoverIdx)}
                x2={xAt(hoverIdx)}
                y1={y0}
                y2={y1}
                stroke="var(--ring)"
                strokeWidth={1}
                strokeDasharray="3 3"
              />
              {(stacked
                ? stacks!.map((s) => ({ color: s.ser.color, y: yAt(s.upper[hoverIdx]) }))
                : series.map((s) => ({ color: s.color, y: yAt(s.values[hoverIdx] ?? 0) }))
              ).map((p, i) => (
                <circle
                  key={i}
                  cx={xAt(hoverIdx)}
                  cy={p.y}
                  r={3.5}
                  fill={p.color}
                  stroke="var(--card)"
                  strokeWidth={1.5}
                />
              ))}
            </g>
          )}
        </svg>
      )}

      {/* tooltip */}
      {hoverIdx !== null && hasData && (
        <div
          className="border-border bg-popover text-popover-foreground pointer-events-none absolute top-1 z-10 min-w-32 rounded-md border px-2.5 py-1.5 text-xs shadow-md"
          style={{
            left: Math.min(Math.max(tooltipLeft + 10, 4), Math.max(4, width - 140)),
          }}
        >
          <div className="text-muted-foreground mb-1 tabular-nums">
            {new Date(times[hoverIdx]).toLocaleTimeString()}
          </div>
          {series.map((ser) => (
            <div key={ser.key} className="flex items-center justify-between gap-3">
              <span className="flex items-center gap-1.5">
                <span
                  className="inline-block size-2 rounded-[2px]"
                  style={{ background: ser.color }}
                />
                {ser.label}
              </span>
              <span className="tabular-nums font-medium">
                {valueFormat(ser.values[hoverIdx] ?? 0)}
                {unit ? ` ${unit}` : ''}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/** Legend row with a live current value per series — carries identity so the
 * chart never relies on color alone. */
export function ChartLegend({
  series,
  valueFormat = (n) => `${Math.round(n)}`,
}: {
  series: ChartSeries[]
  valueFormat?: (n: number) => string
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5">
      {series.map((ser) => {
        const last = ser.values[ser.values.length - 1] ?? 0
        return (
          <div key={ser.key} className="flex items-center gap-2 text-sm">
            <span className="inline-block size-2.5 rounded-[3px]" style={{ background: ser.color }} />
            <span className="text-muted-foreground">{ser.label}</span>
            <span className="tabular-nums font-medium">{valueFormat(last)}</span>
          </div>
        )
      })}
    </div>
  )
}
