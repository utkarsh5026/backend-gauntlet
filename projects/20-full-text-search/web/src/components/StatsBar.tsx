import type { EngineStats } from '@/api'

interface Props {
  stats: EngineStats | null
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex flex-col">
      <span className="font-mono text-lg leading-tight font-semibold tabular-nums">{value}</span>
      <span className="text-muted-foreground text-xs">{label}</span>
    </div>
  )
}

/** A compact row of index-wide counters from GET /_stats. `buffered` is docs
 *  indexed but not yet refreshed into a searchable segment — a live illustration
 *  of near-real-time indexing. */
export function StatsBar({ stats }: Props) {
  if (!stats) {
    return (
      <div className="text-muted-foreground bg-card rounded-lg border px-4 py-3 text-sm">
        Loading index stats…
      </div>
    )
  }
  return (
    <div className="bg-card flex flex-wrap items-center gap-x-8 gap-y-3 rounded-lg border px-5 py-3">
      <Stat label="documents" value={stats.total_docs.toLocaleString()} />
      <Stat label="segments" value={stats.total_segments} />
      <Stat label="buffered" value={stats.total_buffered} />
      <Stat label="shards" value={stats.shard_count} />
    </div>
  )
}
