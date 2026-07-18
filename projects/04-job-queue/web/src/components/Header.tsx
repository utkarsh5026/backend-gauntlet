import { Pause, Play } from 'lucide-react'

import { Button } from '@/components/ui/button'
import type { ConnState } from '@/hooks/useMetrics'
import { cn } from '@/lib/utils'

const DOT: Record<ConnState, string> = {
  live: 'bg-chart-done',
  error: 'bg-chart-dead',
  connecting: 'bg-muted-foreground',
}

const INTERVALS: { label: string; ms: number }[] = [
  { label: '0.5s', ms: 500 },
  { label: '1s', ms: 1000 },
  { label: '2s', ms: 2000 },
]

export function Header({
  conn,
  paused,
  onTogglePause,
  intervalMs,
  onIntervalChange,
  queues,
}: {
  conn: ConnState
  paused: boolean
  onTogglePause: () => void
  intervalMs: number
  onIntervalChange: (ms: number) => void
  queues: string[]
}) {
  return (
    <header className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h1 className="flex items-center gap-2.5 text-xl font-semibold tracking-tight">
          Job Queue
          <span className="text-muted-foreground text-sm font-normal">project 04</span>
        </h1>
        <p className="text-muted-foreground mt-1 flex items-center gap-2 text-sm">
          <span className={cn('size-2 rounded-full', DOT[conn], conn === 'live' && 'animate-pulse')} />
          {conn === 'live' ? 'live' : conn === 'error' ? 'disconnected' : 'paused'}
          {queues.length > 0 && (
            <span className="text-muted-foreground/80">· queue: {queues.join(', ')}</span>
          )}
        </p>
      </div>

      <div className="flex items-center gap-2">
        <div className="border-border bg-card flex items-center gap-0.5 rounded-md border p-0.5">
          {INTERVALS.map((it) => (
            <button
              key={it.ms}
              onClick={() => onIntervalChange(it.ms)}
              className={cn(
                'rounded px-2.5 py-1 text-xs font-medium tabular-nums transition-colors',
                intervalMs === it.ms
                  ? 'bg-secondary text-secondary-foreground'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {it.label}
            </button>
          ))}
        </div>
        <Button variant="outline" size="sm" onClick={onTogglePause}>
          {paused ? <Play /> : <Pause />}
          {paused ? 'Resume' : 'Pause'}
        </Button>
      </div>
    </header>
  )
}
