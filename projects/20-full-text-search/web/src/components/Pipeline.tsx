import { ArrowRight, HardDrive, MemoryStick } from 'lucide-react'

import type { EngineStats } from '@/api'

interface Props {
  stats: EngineStats | null
}

/** The one picture that explains the whole engine.
 *
 *  Every confusing word in a search engine — "buffered", "segment", "refresh",
 *  "near-real-time" — is really one idea: a document you just added lives in
 *  memory and CANNOT be found yet; refreshing writes it to disk, and only then
 *  does search see it. Two boxes and an arrow say that better than any label.
 *
 *  The numbers are live, so adding a document makes the left box climb and
 *  refreshing empties it into the right one. Watching that happen is the point.
 */
export function Pipeline({ stats }: Props) {
  const waiting = stats?.total_buffered ?? 0
  const onDisk = stats?.total_docs ?? 0
  const segments = stats?.total_segments ?? 0

  return (
    <section className="bg-card/50 rounded-xl border p-5">
      <h2 className="text-muted-foreground mb-4 text-xs font-medium tracking-wide uppercase">
        Where your documents are right now
      </h2>

      <div className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-center">
        <Box
          icon={<MemoryStick className="size-4" />}
          count={waiting}
          unit={waiting === 1 ? 'document' : 'documents'}
          title="Waiting in memory"
          caption="Added, but search cannot find these yet."
          tone={waiting > 0 ? 'warning' : 'idle'}
        />

        <div className="text-muted-foreground flex shrink-0 flex-col items-center gap-1 px-1">
          <ArrowRight className="size-5 rotate-90 sm:rotate-0" />
          <span className="text-[11px] whitespace-nowrap">refresh</span>
        </div>

        <Box
          icon={<HardDrive className="size-4" />}
          count={onDisk}
          unit={onDisk === 1 ? 'document' : 'documents'}
          title="On disk, searchable"
          caption={`Stored across ${segments} segment file${segments === 1 ? '' : 's'}.`}
          tone={onDisk > 0 ? 'success' : 'idle'}
        />
      </div>
    </section>
  )
}

const TONES = {
  idle: 'border-border bg-background text-muted-foreground',
  warning: 'border-warning/40 bg-warning/5 text-warning',
  success: 'border-success/40 bg-success/5 text-success',
} as const

function Box({
  icon,
  count,
  unit,
  title,
  caption,
  tone,
}: {
  icon: React.ReactNode
  count: number
  unit: string
  title: string
  caption: string
  tone: keyof typeof TONES
}) {
  return (
    <div className={`flex-1 rounded-lg border p-4 transition-colors ${TONES[tone]}`}>
      <div className="mb-2 flex items-center gap-2">
        {icon}
        <span className="text-foreground text-sm font-medium">{title}</span>
      </div>
      <div className="flex items-baseline gap-1.5">
        <span className="text-foreground font-mono text-3xl leading-none font-semibold tabular-nums">
          {count.toLocaleString()}
        </span>
        <span className="text-muted-foreground text-sm">{unit}</span>
      </div>
      <p className="text-muted-foreground mt-2 text-xs leading-relaxed">{caption}</p>
    </div>
  )
}
