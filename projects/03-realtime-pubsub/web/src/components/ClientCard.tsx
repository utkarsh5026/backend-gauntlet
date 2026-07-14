import { useState } from 'react'
import {
  ArrowDownLeft,
  ArrowUpRight,
  CircleAlert,
  Info,
  Plug,
  Power,
  Send,
  Trash2,
  Users,
  X,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { store } from '@/lib/store'
import { cn } from '@/lib/utils'
import { clockTime, fmtLatency, fmtNum } from '@/lib/format'
import type { ClientSnapshot, LogEntry, Status } from '@/lib/store'

const statusMeta: Record<Status, { dot: string; label: string; badge: string }> = {
  open: { dot: 'bg-success', label: 'connected', badge: 'text-success border-success/40' },
  connecting: { dot: 'bg-amber-400 animate-pulse', label: 'connecting', badge: 'text-amber-400 border-amber-400/40' },
  closed: { dot: 'bg-muted-foreground/50', label: 'closed', badge: 'text-muted-foreground border-border' },
  error: { dot: 'bg-destructive', label: 'error', badge: 'text-destructive border-destructive/40' },
}

function Stat({ label, value, danger }: { label: string; value: string; danger?: boolean }) {
  return (
    <div className="flex flex-col">
      <span className={cn('font-mono text-lg font-semibold tabular-nums', danger && 'text-destructive')}>{value}</span>
      <span className="text-muted-foreground text-[11px] uppercase tracking-wide">{label}</span>
    </div>
  )
}

function LogRow({ e }: { e: LogEntry }) {
  const icon =
    e.kind === 'error' ? (
      <CircleAlert className="text-destructive size-3 shrink-0" />
    ) : e.kind === 'system' ? (
      <Info className="text-muted-foreground size-3 shrink-0" />
    ) : e.kind === 'presence' ? (
      <Users className="size-3 shrink-0 text-sky-400" />
    ) : e.dir === 'out' ? (
      <ArrowUpRight className="size-3 shrink-0 text-primary" />
    ) : (
      <ArrowDownLeft className="text-success size-3 shrink-0" />
    )
  return (
    <div className="flex items-start gap-1.5 py-0.5 leading-snug">
      <span className="text-muted-foreground/60 shrink-0 tabular-nums">{clockTime(e.at).slice(0, 12)}</span>
      {icon}
      {e.topic && <span className="text-muted-foreground shrink-0">{e.topic}</span>}
      <span
        className={cn(
          'min-w-0 break-words',
          e.kind === 'error' && 'text-destructive',
          e.kind === 'system' && 'text-muted-foreground italic',
          e.kind === 'presence' && 'text-sky-400',
        )}
      >
        {e.from && e.kind === 'message' && <span className="text-muted-foreground">{e.from}: </span>}
        {e.text}
      </span>
      {e.latencyMs !== undefined && (
        <span className="text-muted-foreground/70 ml-auto shrink-0 pl-1 tabular-nums">{fmtLatency(e.latencyMs)}</span>
      )}
    </div>
  )
}

export function ClientCard({ c }: { c: ClientSnapshot }) {
  const [topicInput, setTopicInput] = useState('')
  const [pubTopic, setPubTopic] = useState('')
  const [pubMsg, setPubMsg] = useState('')
  const meta = statusMeta[c.status]
  const connected = c.status === 'open'
  const effectiveTopic = pubTopic.trim() || c.subscriptions[0] || ''

  const addTopic = () => {
    if (topicInput.trim()) {
      store.subscribeTopic(c.id, topicInput)
      setTopicInput('')
    }
  }
  const publish = () => {
    if (effectiveTopic) {
      store.publish(c.id, effectiveTopic, pubMsg.trim() || 'hello')
      setPubMsg('')
    }
  }

  return (
    <Card className="gap-0 overflow-hidden py-0">
      {/* header */}
      <CardHeader className="grid-cols-[auto_1fr_auto] items-center gap-2 border-b py-3 [.border-b]:pb-3">
        <span className={cn('size-2.5 rounded-full', meta.dot)} />
        <input
          value={c.name}
          onChange={(e) => store.renameClient(c.id, e.target.value)}
          spellCheck={false}
          className="min-w-0 bg-transparent font-medium outline-none focus:underline"
        />
        <div className="flex items-center gap-1">
          <Badge variant="outline" className={cn('font-mono', meta.badge)}>
            {meta.label}
          </Badge>
          {connected ? (
            <Button size="icon" variant="ghost" className="size-7" title="disconnect" onClick={() => store.disconnect(c.id)}>
              <Power className="size-3.5" />
            </Button>
          ) : (
            <Button size="icon" variant="ghost" className="size-7" title="connect" onClick={() => store.connect(c.id)}>
              <Plug className="size-3.5" />
            </Button>
          )}
          <Button
            size="icon"
            variant="ghost"
            className="text-muted-foreground hover:text-destructive size-7"
            title="remove client"
            onClick={() => store.removeClient(c.id)}
          >
            <Trash2 className="size-3.5" />
          </Button>
        </div>
      </CardHeader>

      <CardContent className="flex flex-col gap-3 py-3">
        {c.error && c.status === 'error' && <div className="text-destructive text-xs">{c.error}</div>}

        {/* stats */}
        <div className="grid grid-cols-4 gap-2">
          <Stat label="recv" value={fmtNum(c.received)} />
          <Stat label="rate/s" value={fmtNum(c.ratePerSec)} />
          <Stat label="drops" value={fmtNum(c.droppedGaps)} danger={c.droppedGaps > 0} />
          <Stat label="latency" value={fmtLatency(c.avgLatencyMs)} />
        </div>

        {/* subscriptions */}
        <div className="flex flex-wrap items-center gap-1.5">
          {c.subscriptions.map((t) => (
            <Badge key={t} variant="secondary" className="gap-1 font-mono">
              {t}
              <button className="hover:text-destructive" onClick={() => store.unsubscribeTopic(c.id, t)} title="unsubscribe">
                <X className="size-3" />
              </button>
            </Badge>
          ))}
          <div className="flex items-center gap-1">
            <Input
              value={topicInput}
              onChange={(e) => setTopicInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && addTopic()}
              placeholder="+ topic"
              className="h-7 w-24 font-mono text-xs"
              spellCheck={false}
            />
          </div>
        </div>

        {/* publish */}
        <div className="flex gap-1.5">
          <Input
            value={pubTopic}
            onChange={(e) => setPubTopic(e.target.value)}
            placeholder={c.subscriptions[0] ?? 'topic'}
            className="h-8 w-28 shrink-0 font-mono text-xs"
            spellCheck={false}
          />
          <Input
            value={pubMsg}
            onChange={(e) => setPubMsg(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && publish()}
            placeholder="message…"
            className="h-8 text-xs"
            disabled={!connected}
          />
          <Button size="sm" className="h-8 shrink-0" onClick={publish} disabled={!connected || !effectiveTopic}>
            <Send className="size-3.5" />
          </Button>
        </div>

        {/* log */}
        <div className="bg-muted/30 h-52 overflow-y-auto rounded-md border p-2 font-mono text-[11px]">
          {c.log.length === 0 ? (
            <p className="text-muted-foreground/60 py-4 text-center">no traffic yet</p>
          ) : (
            c.log.map((e) => <LogRow key={e.id} e={e} />)
          )}
        </div>
      </CardContent>
    </Card>
  )
}
