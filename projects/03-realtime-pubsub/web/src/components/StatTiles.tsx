import { Activity, PlugZap, Radio, TriangleAlert, Waves } from 'lucide-react'

import { Card } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import { fmtNum, fmtRate } from '@/lib/format'
import type { Totals } from '@/lib/store'

function Tile({
  icon,
  label,
  value,
  sub,
  danger,
}: {
  icon: React.ReactNode
  label: string
  value: string
  sub?: string
  danger?: boolean
}) {
  return (
    <Card className="gap-0 py-4">
      <div className="flex items-center justify-between px-4">
        <span className="text-muted-foreground text-xs font-medium tracking-wide uppercase">{label}</span>
        <span className={cn('text-muted-foreground', danger && 'text-destructive')}>{icon}</span>
      </div>
      <div className="px-4 pt-1">
        <div className={cn('font-mono text-2xl font-semibold tabular-nums', danger && 'text-destructive')}>{value}</div>
        {sub && <div className="text-muted-foreground mt-0.5 text-xs">{sub}</div>}
      </div>
    </Card>
  )
}

export function StatTiles({ totals }: { totals: Totals }) {
  const amp = totals.publishRate > 0 ? (totals.deliverRate / totals.publishRate).toFixed(1) : '—'
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      <Tile
        icon={<PlugZap className="size-4" />}
        label="Clients"
        value={`${totals.connected}/${totals.clients}`}
        sub="connected / total"
      />
      <Tile icon={<Radio className="size-4" />} label="Topics" value={fmtNum(totals.topics)} sub="rooms in play" />
      <Tile
        icon={<Activity className="size-4" />}
        label="Publish"
        value={fmtRate(totals.publishRate)}
        sub={`${fmtNum(totals.published)} sent`}
      />
      <Tile
        icon={<Waves className="size-4" />}
        label="Fan-out"
        value={fmtRate(totals.deliverRate)}
        sub={`${amp}× amplification · ${fmtNum(totals.delivered)} delivered`}
      />
      <Tile
        icon={<TriangleAlert className="size-4" />}
        label="Dropped"
        value={fmtNum(totals.dropped)}
        sub="seq gaps (V2 shedding)"
        danger={totals.dropped > 0}
      />
    </div>
  )
}
