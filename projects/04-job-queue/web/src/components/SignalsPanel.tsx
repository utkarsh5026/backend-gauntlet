import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import type { DerivedSnapshot } from '@/hooks/useMetrics'
import { fmtInt, fmtMs } from '@/lib/format'

function Row({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5" title={hint}>
      <span className="text-muted-foreground text-sm">{label}</span>
      <span className="tabular-nums font-medium">{value}</span>
    </div>
  )
}

/** The counters/histograms that aren't headline gauges but tell you *why* the
 * queue behaves as it does — the SPEC's "reap rate means workers dying", the
 * empty-claim busy-poll cost (V4), and the execution-time distribution. */
export function SignalsPanel({ latest }: { latest: DerivedSnapshot | null }) {
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Signals</CardTitle>
        <CardDescription>Retry, recovery &amp; latency counters.</CardDescription>
      </CardHeader>
      <CardContent className="divide-border divide-y">
        <Row
          label="Retried"
          value={fmtInt(latest?.retried ?? 0)}
          hint="Failures rescheduled with backoff (still had attempts left)."
        />
        <Row
          label="Dead-lettered"
          value={fmtInt(latest?.deadLettered ?? 0)}
          hint="Failures that exhausted max_attempts and landed in the DLQ."
        />
        <Row
          label="Leases reaped"
          value={fmtInt(latest?.leasesReaped ?? 0)}
          hint="Expired leases the reaper returned to ready — a non-zero rate means workers are dying or the lease is too short."
        />
        <Row
          label="Empty claims"
          value={fmtInt(latest?.claimsEmpty ?? 0)}
          hint="Claims that found no work — the busy-poll cost that V4's LISTEN/NOTIFY exists to cut."
        />
        <Row
          label="Exec p50 / p99"
          value={`${fmtMs(latest?.execP50Ms)} / ${fmtMs(latest?.execP99Ms)}`}
          hint="Handler execution time distribution."
        />
        <Row
          label="End-to-end p50 / p99"
          value={`${fmtMs(latest?.e2eP50Ms)} / ${fmtMs(latest?.e2eP99Ms)}`}
          hint="Enqueue → done latency. Absent unless the backend records the e2e histogram."
        />
      </CardContent>
    </Card>
  )
}
