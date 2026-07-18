import { useState } from 'react'
import { Droplets, Loader2, Send, Skull, Timer, Zap } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { enqueue, getToken, setToken, type NewJob } from '@/lib/api'
import { cn } from '@/lib/utils'

/** Enqueue `count` jobs from a factory with bounded concurrency, so a 500-job
 * flood doesn't open 500 sockets at once. Returns {ok, fail}. */
async function enqueueMany(
  factory: (i: number) => NewJob,
  count: number,
  concurrency = 16,
  onProgress?: (done: number) => void,
): Promise<{ ok: number; fail: number; firstError?: string }> {
  let next = 0
  let ok = 0
  let fail = 0
  let done = 0
  let firstError: string | undefined

  async function worker() {
    while (next < count) {
      const i = next++
      try {
        await enqueue(factory(i))
        ok++
      } catch (e) {
        fail++
        if (!firstError) firstError = e instanceof Error ? e.message : String(e)
      }
      done++
      onProgress?.(done)
    }
  }

  await Promise.all(Array.from({ length: Math.min(concurrency, count) }, worker))
  return { ok, fail, firstError }
}

type Status = { kind: 'idle' } | { kind: 'running'; done: number; total: number } | { kind: 'done'; msg: string; error?: boolean }

const KINDS = ['sleep', 'noop', 'echo', 'fail', 'flaky_then_ok', 'webhook'] as const
type Kind = (typeof KINDS)[number]

const DEFAULT_PAYLOAD: Record<Kind, string> = {
  sleep: '{ "ms": 400 }',
  noop: 'null',
  echo: '{ "msg": "hello" }',
  fail: 'null',
  flaky_then_ok: '{ "fail_n": 2 }',
  webhook: '{ "url": "https://example.com" }',
}

export function LoadGenerator({ onEnqueued }: { onEnqueued?: () => void }) {
  const [queue, setQueue] = useState('default')
  const [status, setStatus] = useState<Status>({ kind: 'idle' })
  const [token, setTok] = useState(getToken())

  // custom form
  const [kind, setKind] = useState<Kind>('sleep')
  const [payload, setPayload] = useState(DEFAULT_PAYLOAD.sleep)
  const [count, setCount] = useState(1)
  const [delay, setDelay] = useState(0)

  const running = status.kind === 'running'

  async function run(label: string, factory: (i: number) => NewJob, total: number) {
    setStatus({ kind: 'running', done: 0, total })
    const res = await enqueueMany(factory, total, 16, (done) =>
      setStatus({ kind: 'running', done, total }),
    )
    onEnqueued?.()
    if (res.fail > 0) {
      setStatus({
        kind: 'done',
        error: true,
        msg: `${label}: ${res.ok} ok, ${res.fail} failed — ${res.firstError ?? ''}`,
      })
    } else {
      setStatus({ kind: 'done', msg: `${label}: enqueued ${res.ok}` })
    }
  }

  const onKind = (k: Kind) => {
    setKind(k)
    setPayload(DEFAULT_PAYLOAD[k])
  }

  async function submitCustom() {
    let parsed: unknown = null
    try {
      parsed = payload.trim() === '' ? null : JSON.parse(payload)
    } catch {
      setStatus({ kind: 'done', error: true, msg: 'payload is not valid JSON' })
      return
    }
    const n = Math.max(1, Math.min(5000, Math.floor(count) || 1))
    await run(`${n}× ${kind}`, () => ({
      queue,
      kind,
      payload: parsed,
      ...(delay > 0 ? { delay_secs: Math.floor(delay) } : {}),
    }), n)
  }

  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Load generator</CardTitle>
        <CardDescription>Feed the queue and watch it react above.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* quick actions */}
        <div className="grid grid-cols-2 gap-2">
          <Button
            variant="outline"
            disabled={running}
            onClick={() => run('Flood', () => ({ queue, kind: 'sleep', payload: { ms: 400 } }), 200)}
          >
            <Droplets /> Flood 200
          </Button>
          <Button
            variant="outline"
            disabled={running}
            onClick={() => run('Burst', () => ({ queue, kind: 'noop', payload: null }), 500)}
          >
            <Zap /> Burst 500 noop
          </Button>
          <Button
            variant="outline"
            disabled={running}
            onClick={() =>
              run('Poison', () => ({ queue, kind: 'fail', payload: null, max_attempts: 3 }), 1)
            }
          >
            <Skull /> Poison (→ DLQ)
          </Button>
          <Button
            variant="outline"
            disabled={running}
            onClick={() =>
              run('Delayed', () => ({ queue, kind: 'sleep', payload: { ms: 200 }, delay_secs: 10 }), 5)
            }
          >
            <Timer /> Delayed 10s
          </Button>
        </div>

        {/* custom enqueue */}
        <div className="border-border space-y-3 border-t pt-4">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="lg-queue">Queue</Label>
              <Input id="lg-queue" value={queue} onChange={(e) => setQueue(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="lg-kind">Kind</Label>
              <select
                id="lg-kind"
                value={kind}
                onChange={(e) => onKind(e.target.value as Kind)}
                className="border-input bg-transparent dark:bg-input/30 h-9 w-full rounded-md border px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]"
              >
                {KINDS.map((k) => (
                  <option key={k} value={k} className="bg-popover">
                    {k}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="lg-payload">Payload (JSON)</Label>
            <textarea
              id="lg-payload"
              value={payload}
              onChange={(e) => setPayload(e.target.value)}
              spellCheck={false}
              rows={2}
              className="border-input bg-transparent dark:bg-input/30 font-mono w-full resize-y rounded-md border px-3 py-2 text-xs outline-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="lg-count">Count</Label>
              <Input
                id="lg-count"
                type="number"
                min={1}
                max={5000}
                value={count}
                onChange={(e) => setCount(Number(e.target.value))}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="lg-delay">Delay (s)</Label>
              <Input
                id="lg-delay"
                type="number"
                min={0}
                value={delay}
                onChange={(e) => setDelay(Number(e.target.value))}
              />
            </div>
          </div>

          <Button className="w-full" disabled={running} onClick={submitCustom}>
            {running ? <Loader2 className="animate-spin" /> : <Send />}
            Enqueue
          </Button>
        </div>

        {/* auth token */}
        <div className="border-border space-y-1.5 border-t pt-4">
          <Label htmlFor="lg-token">Enqueue token</Label>
          <Input
            id="lg-token"
            type="password"
            placeholder="ENQUEUE_TOKEN (blank if auth disabled)"
            value={token}
            onChange={(e) => {
              setTok(e.target.value)
              setToken(e.target.value)
            }}
          />
          <p className="text-muted-foreground text-xs">
            Sent as <code className="font-mono">Bearer</code> on enqueue &amp; requeue. Stored only in
            your browser.
          </p>
        </div>

        {/* status line */}
        {status.kind !== 'idle' && (
          <div
            className={cn(
              'rounded-md border px-3 py-2 text-xs tabular-nums',
              status.kind === 'done' && status.error
                ? 'border-chart-dead/40 text-chart-dead'
                : 'border-border text-muted-foreground',
            )}
          >
            {status.kind === 'running'
              ? `enqueuing… ${status.done}/${status.total}`
              : status.msg}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
