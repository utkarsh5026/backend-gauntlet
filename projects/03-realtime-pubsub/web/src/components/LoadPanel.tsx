import { Gauge, Play, Square } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { store } from '@/lib/store'
import { fmtNum, fmtRate } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { Snapshot } from '@/lib/store'

export function LoadPanel({ snap }: { snap: Snapshot }) {
  const { load, clients, totals } = snap
  const openClients = clients.filter((c) => c.status === 'open')
  const room = snap.rooms.find((r) => r.topic === load.topic)
  const subs = room?.subscriberIds.length ?? 0

  return (
    <Card className="gap-4">
      <CardHeader className="[.border-b]:pb-4 border-b">
        <CardTitle className="flex items-center gap-2">
          <Gauge className="size-4" /> Firehose
          {load.running && (
            <Badge className="ml-auto animate-pulse bg-success text-white">live</Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-2">
          <div className="flex flex-col gap-1.5">
            <Label className="text-xs">Sender</Label>
            <select
              value={load.senderId ?? ''}
              onChange={(e) => store.configureLoad({ senderId: e.target.value })}
              disabled={load.running}
              className="border-input bg-transparent dark:bg-input/30 h-8 rounded-md border px-2 font-mono text-sm outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:opacity-50"
            >
              <option value="">auto</option>
              {openClients.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label className="text-xs">Topic</Label>
            <Input
              value={load.topic}
              onChange={(e) => store.configureLoad({ topic: e.target.value })}
              disabled={load.running}
              className="h-8 font-mono text-sm"
              spellCheck={false}
            />
          </div>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label className="text-xs">
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
          disabled={!load.running && openClients.length === 0}
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

        <div className="text-muted-foreground grid grid-cols-3 gap-2 border-t pt-3 text-center text-xs">
          <Metric label="sent" value={fmtNum(load.sent)} />
          <Metric label="publish" value={fmtRate(totals.publishRate)} />
          <Metric label="fan-out" value={fmtRate(totals.deliverRate)} accent={subs > 1} />
        </div>
        <p className="text-muted-foreground text-xs leading-relaxed">
          Publishing <span className="font-mono">{load.rate}/s</span> to{' '}
          <span className="font-mono">{load.topic}</span> ({subs} subscriber{subs === 1 ? '' : 's'}). Watch each
          subscriber's <span className="text-foreground">rate</span> track the source, and <span className="text-destructive">drops</span>{' '}
          climb if the server sheds under backpressure.
        </p>
      </CardContent>
    </Card>
  )
}

function Metric({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div>
      <div className={cn('text-foreground font-mono text-sm font-semibold tabular-nums', accent && 'text-success')}>
        {value}
      </div>
      <div className="uppercase tracking-wide">{label}</div>
    </div>
  )
}
