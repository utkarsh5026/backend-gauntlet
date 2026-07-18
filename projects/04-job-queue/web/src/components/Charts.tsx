import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ChartLegend, TimeChart, type ChartSeries } from '@/components/TimeChart'
import type { DerivedSnapshot } from '@/hooks/useMetrics'
import { fmtInt } from '@/lib/format'

const fmtRate1 = (n: number) => (n < 10 ? n.toFixed(1) : String(Math.round(n)))

/** Ready + running, stacked — the total work sitting in the system over time.
 * On a flood this rises; as workers drain it, it falls back to the floor. */
export function DepthChart({ history }: { history: DerivedSnapshot[] }) {
  const times = history.map((h) => h.t)
  const series: ChartSeries[] = [
    { key: 'running', label: 'Running', color: 'var(--chart-running)', values: history.map((h) => h.running) },
    { key: 'ready', label: 'Ready', color: 'var(--chart-ready)', values: history.map((h) => h.ready) },
  ]
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Queue depth</CardTitle>
        <CardDescription>Work in the system — ready backlog + running, stacked.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <TimeChart series={series} times={times} stacked valueFormat={fmtInt} unit="jobs" />
        <ChartLegend series={series} valueFormat={fmtInt} />
      </CardContent>
    </Card>
  )
}

/** Enqueue rate vs. completion rate. When completion sits below enqueue the
 * backlog is growing; when it climbs above, the queue is draining. */
export function ThroughputChart({ history }: { history: DerivedSnapshot[] }) {
  const times = history.map((h) => h.t)
  const series: ChartSeries[] = [
    { key: 'enqueue', label: 'Enqueued/s', color: 'var(--chart-ready)', values: history.map((h) => h.enqueueRate) },
    { key: 'complete', label: 'Completed/s', color: 'var(--chart-done)', values: history.map((h) => h.completeRate) },
  ]
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Throughput</CardTitle>
        <CardDescription>Enqueue vs. completion rate — is the pool keeping up?</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <TimeChart series={series} times={times} valueFormat={fmtRate1} unit="/s" />
        <ChartLegend series={series} valueFormat={(n) => `${fmtRate1(n)}/s`} />
      </CardContent>
    </Card>
  )
}
