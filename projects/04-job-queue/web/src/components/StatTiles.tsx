import type { DerivedSnapshot } from '@/hooks/useMetrics'
import { fmtDuration, fmtInt, fmtRate } from '@/lib/format'
import { cn } from '@/lib/utils'

const COLOR = {
  ready: 'var(--chart-ready)',
  running: 'var(--chart-running)',
  done: 'var(--chart-done)',
  dead: 'var(--chart-dead)',
} as const

/** A tiny baseline-anchored sparkline — magnitude at a glance, no axes. */
function Sparkline({ values, color }: { values: number[]; color: string }) {
  const w = 96
  const h = 28
  if (values.length < 2) return <svg width={w} height={h} />
  const max = Math.max(1, ...values)
  const step = w / (values.length - 1)
  const pts = values.map((v, i) => `${i * step},${h - (v / max) * (h - 4) - 2}`).join(' ')
  return (
    <svg width={w} height={h} className="overflow-visible">
      <polyline
        points={pts}
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  )
}

function Tile({
  label,
  value,
  sub,
  color,
  spark,
  alert,
}: {
  label: string
  value: string
  sub?: string
  color: string
  spark?: number[]
  alert?: boolean
}) {
  return (
    <div
      className={cn(
        'bg-card relative flex flex-col gap-2 overflow-hidden rounded-xl border p-4',
        alert && 'border-chart-dead/40',
      )}
    >
      <div className="flex items-center gap-2">
        <span className="size-2 rounded-[3px]" style={{ background: color }} />
        <span className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
          {label}
        </span>
      </div>
      <div className="flex items-end justify-between gap-2">
        <div>
          <div className="text-2xl font-semibold tabular-nums leading-none">{value}</div>
          {sub && <div className="text-muted-foreground mt-1.5 text-xs tabular-nums">{sub}</div>}
        </div>
        {spark && (
          <div className="opacity-80">
            <Sparkline values={spark} color={color} />
          </div>
        )}
      </div>
    </div>
  )
}

export function StatTiles({ history, latest }: { history: DerivedSnapshot[]; latest: DerivedSnapshot | null }) {
  const readyHist = history.map((h) => h.ready)
  const runningHist = history.map((h) => h.running)
  const doneHist = history.map((h) => h.completed)

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
      <Tile
        label="Ready"
        value={fmtInt(latest?.ready ?? 0)}
        sub="backlog"
        color={COLOR.ready}
        spark={readyHist}
      />
      <Tile
        label="Running"
        value={fmtInt(latest?.running ?? 0)}
        sub="in-flight"
        color={COLOR.running}
        spark={runningHist}
      />
      <Tile
        label="Completed"
        value={fmtInt(latest?.completed ?? 0)}
        sub={`${fmtRate(latest?.completeRate)} now`}
        color={COLOR.done}
        spark={doneHist}
      />
      <Tile
        label="Dead-letter"
        value={fmtInt(latest?.dlq ?? 0)}
        sub={`${fmtInt(latest?.deadLettered ?? 0)} total`}
        color={COLOR.dead}
        alert={(latest?.dlq ?? 0) > 0}
      />
      <Tile
        label="Lag"
        value={fmtDuration(latest?.lagSeconds ?? 0)}
        sub="oldest ready"
        color={COLOR.ready}
      />
      <Tile
        label="Throughput"
        value={fmtRate(latest?.completeRate)}
        sub={`enqueue ${fmtRate(latest?.enqueueRate)}`}
        color={COLOR.running}
      />
    </div>
  )
}
