import { Activity, Gauge, Play, Square, TriangleAlert, Waves } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { DepsHealth } from '@/components/DepsHealth'
import { store } from '@/lib/store'
import { fmtLatency, fmtNum, fmtRate } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { Snapshot } from '@/lib/store'

function Stat({ label, value, danger }: { label: string; value: string; danger?: boolean }) {
  return (
    <div>
      <div className={cn('font-mono text-sm font-semibold tabular-nums', danger && 'text-destructive')}>{value}</div>
      <div className="text-muted-foreground text-[10px] tracking-wide uppercase">{label}</div>
    </div>
  )
}

export function DevPanel({ snap }: { snap: Snapshot }) {
  const { totals, load } = snap
  const room = snap.rooms.find((r) => r.topic === load.topic)
  const subs = room ? Math.max(1, room.members.length) : 0
  const amp = totals.publishRate > 0 ? (totals.deliverRate / totals.publishRate).toFixed(1) : '—'

  return (
    <aside className="bg-card flex w-80 shrink-0 flex-col gap-4 overflow-y-auto border-l p-4">
      <DepsHealth />

      <div>
        <h3 className="text-muted-foreground mb-2 text-xs font-medium tracking-wide uppercase">
          What the protocol is doing
        </h3>
        <div className="grid grid-cols-2 gap-3">
          <Stat label="published" value={fmtNum(totals.published)} />
          <Stat label="received" value={fmtNum(totals.received)} />
          <Stat label="publish/s" value={fmtRate(totals.publishRate)} />
          <Stat label="deliver/s" value={fmtRate(totals.deliverRate)} />
          <Stat label="avg latency" value={fmtLatency(totals.avgLatencyMs)} />
          <Stat label="dropped gaps" value={fmtNum(totals.droppedGaps)} danger={totals.droppedGaps > 0} />
        </div>
        <p className="text-muted-foreground mt-2 text-[11px] leading-relaxed">
          <span className="inline-flex items-center gap-1">
            <Activity className="size-3" /> deliver/s
          </span>{' '}
          &gt; publish/s means fan-out (multiple rooms/tabs receiving one publish).{' '}
          <span className="text-destructive inline-flex items-center gap-1">
            <TriangleAlert className="size-3" /> dropped gaps
          </span>{' '}
          are holes in the per-sender sequence — the server shedding under backpressure (V2).
        </p>
      </div>

      <Card className="gap-3 py-4">
        <CardHeader className="gap-0 px-4 [.border-b]:pb-3">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Gauge className="size-4" /> Firehose
            {load.running && <span className="bg-success ml-auto size-2 animate-pulse rounded-full" />}
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 px-4">
          <div className="flex flex-col gap-1.5">
            <Label className="text-[11px]">Topic</Label>
            <Input
              value={load.topic}
              onChange={(e) => store.configureLoad({ topic: e.target.value })}
              disabled={load.running}
              className="h-8 font-mono text-sm"
              spellCheck={false}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label className="text-[11px]">
              Rate <span className="text-muted-foreground font-mono font-normal">{load.rate}/s</span>
            </Label>
            <input
              type="range"
              min={1}
              max={2000}
              step={1}
              value={load.rate}
              onChange={(e) => store.configureLoad({ rate: Number(e.target.value) })}
              className="accent-primary h-2 w-full cursor-pointer"
            />
          </div>
          <Button
            onClick={() => (load.running ? store.stopLoad() : store.startLoad())}
            disabled={!load.running && snap.status !== 'open'}
            variant={load.running ? 'destructive' : 'default'}
            className="w-full"
          >
            {load.running ? (
              <>
                <Square /> Stop
              </>
            ) : (
              <>
                <Play /> Start firehose
              </>
            )}
          </Button>
          <div className="text-muted-foreground grid grid-cols-2 gap-2 border-t pt-3 text-center text-xs">
            <div>
              <div className="text-foreground font-mono text-sm font-semibold tabular-nums">{fmtNum(load.sent)}</div>
              <div className="uppercase tracking-wide">sent</div>
            </div>
            <div>
              <div className={cn('font-mono text-sm font-semibold tabular-nums', subs > 1 && 'text-success')}>{amp}×</div>
              <div className="flex items-center justify-center gap-1 uppercase tracking-wide">
                <Waves className="size-3" /> amplification
              </div>
            </div>
          </div>
          <p className="text-muted-foreground text-[11px] leading-relaxed">
            Publishes to <span className="font-mono">{load.topic}</span> as you, whether or not you're in that room.
            Join it to watch the flood land in your own thread; open a second tab in the same room to see it fan out
            to someone else.
          </p>
        </CardContent>
      </Card>
    </aside>
  )
}
