import type { ReactNode } from 'react'
import { AlertTriangle, WifiOff } from 'lucide-react'

import { DepthChart, ThroughputChart } from '@/components/Charts'
import { DlqPanel } from '@/components/DlqPanel'
import { Header } from '@/components/Header'
import { LoadGenerator } from '@/components/LoadGenerator'
import { SignalsPanel } from '@/components/SignalsPanel'
import { StatTiles } from '@/components/StatTiles'
import { useMetrics } from '@/hooks/useMetrics'
import { cn } from '@/lib/utils'

function Banner({
  tone,
  icon,
  children,
}: {
  tone: 'error' | 'warn'
  icon: ReactNode
  children: ReactNode
}) {
  return (
    <div
      className={cn(
        'flex items-start gap-3 rounded-lg border px-4 py-3 text-sm',
        tone === 'error'
          ? 'border-chart-dead/40 bg-chart-dead/5 text-foreground'
          : 'border-chart-ready/40 bg-chart-ready/5 text-foreground',
      )}
    >
      <span className={cn('mt-0.5 shrink-0', tone === 'error' ? 'text-chart-dead' : 'text-chart-ready')}>
        {icon}
      </span>
      <div className="[&_code]:font-mono [&_code]:text-xs">{children}</div>
    </div>
  )
}

export default function App() {
  const m = useMetrics()
  const { latest, conn } = m

  const showError = conn === 'error'
  const noData = !showError && latest !== null && !latest.hasAny
  const workersOff = !showError && latest !== null && latest.hasAny && !latest.hasGauges

  return (
    <div className="bg-background text-foreground min-h-screen">
      <div className="mx-auto max-w-7xl space-y-6 px-4 py-6 sm:px-6">
        <Header
          conn={conn}
          paused={m.paused}
          onTogglePause={() => m.setPaused(!m.paused)}
          intervalMs={m.intervalMs}
          onIntervalChange={m.setIntervalMs}
          queues={latest?.queues ?? []}
        />

        {showError && (
          <Banner tone="error" icon={<WifiOff className="size-4" />}>
            Can't reach <code>GET /metrics</code>. Is the backend running? Start it with{' '}
            <code>cargo run -p job-queue</code> (the dev proxy forwards to{' '}
            <code>localhost:8080</code>). {m.error && <span className="text-muted-foreground">— {m.error}</span>}
          </Banner>
        )}
        {noData && (
          <Banner tone="warn" icon={<AlertTriangle className="size-4" />}>
            Connected, but no <code>job_queue_*</code> metrics have been emitted yet. Enqueue a job
            from the load generator to bring the dashboard to life.
          </Banner>
        )}
        {workersOff && (
          <Banner tone="warn" icon={<AlertTriangle className="size-4" />}>
            Counters are flowing but the depth gauges are empty — the gauge sampler and worker pool
            only run with <code>RUN_WORKERS=true</code>. Restart the server that way, or jobs will
            pile up in <code>ready</code> and never drain.
          </Banner>
        )}

        <StatTiles history={m.history} latest={latest} />

        <div className="grid gap-6 lg:grid-cols-3">
          <div className="space-y-6 lg:col-span-2">
            <DepthChart history={m.history} />
            <ThroughputChart history={m.history} />
          </div>
          <div className="space-y-6">
            <LoadGenerator />
            <SignalsPanel latest={latest} />
          </div>
        </div>

        <DlqPanel />

        <footer className="text-muted-foreground border-border border-t pt-4 text-xs">
          Pure client — reads <code className="font-mono">/metrics</code>, drives{' '}
          <code className="font-mono">/jobs</code>, <code className="font-mono">/dlq</code>,{' '}
          <code className="font-mono">/job/:id/requeue</code>. No backend changes.
        </footer>
      </div>
    </div>
  )
}
